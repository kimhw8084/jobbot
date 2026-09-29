from __future__ import annotations

import hashlib
import http.client
import io
import json
import math
import os
import socket
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol

from ..search_plan import SearchTask
from .models import (
    AcquisitionRecord, ProviderBatch, ProviderCompletionState, ProviderFailure,
    ProviderFailureClass,
)


API_ROOT = "https://api.brightdata.com"
_HTTPS_CONNECTION_TYPE = http.client.HTTPSConnection
TOKEN_ENV = "BRIGHTDATA_API_TOKEN"
TASK_TIMEOUT_ENV = "JOBBOT_BRIGHTDATA_TASK_TIMEOUT_SECONDS"
DEFAULT_TASK_TIMEOUT_SECONDS = 300.0
DEFAULT_HTTP_TIMEOUT_SECONDS = 45.0
PLATFORM_ENV = {
    "linkedin": "JOBBOT_BRIGHTDATA_LINKEDIN_CONFIG",
    "indeed": "JOBBOT_BRIGHTDATA_INDEED_CONFIG",
    "glassdoor": "JOBBOT_BRIGHTDATA_GLASSDOOR_CONFIG",
}
SUPPORTED_OUTPUT_FIELDS = frozenset({
    "provider_record_id", "source_job_id", "source_url", "title", "company", "location",
    "posted_date", "posted_text", "employment_type", "description", "summary", "salary",
    "application_url", "employer_job_url", "ats_requisition_url",
})
TRUST_CLAIM_FIELDS = frozenset({
    "verified_application_url", "source_verification_state",
    "application_destination_verification_state",
})
SECRET_FIELD_PARTS = ("token", "secret", "authorization", "api_key", "apikey", "credential")


class BrightDataConfigurationError(ValueError):
    """A secret-safe message that describes a runtime configuration problem."""


@dataclass(frozen=True)
class BrightDataField:
    field: str
    source: str = ""
    value: Any = None
    template: str = ""
    kind: str = ""


@dataclass(frozen=True)
class BrightDataPlatformConfig:
    platform: str
    dataset_id: str
    keyword_field: str
    location: BrightDataField | None
    remote: BrightDataField | None
    window_days: BrightDataField | None
    output_schema: Mapping[str, str]
    result_rows_key: str = ""


@dataclass(frozen=True)
class BrightDataHTTPResponse:
    status: int
    payload: Any
    headers: Mapping[str, str] = field(default_factory=dict)


class BrightDataTransport(Protocol):
    def request(self, method: str, path: str, *, params: Mapping[str, Any],
                json_body: Any, token: str, timeout_seconds: float,
                task_deadline: float, on_attempt: Callable[[], None]) -> BrightDataHTTPResponse: ...


class _DeadlineSocket:
    """Apply the task deadline to each socket read used by HTTPResponse."""

    def __init__(self, network_socket: Any, timeout_seconds: float,
                 task_deadline: float, monotonic: Callable[[], float]):
        self.network_socket = network_socket
        self.timeout_seconds = timeout_seconds
        self.task_deadline = task_deadline
        self.monotonic = monotonic

    def makefile(self, mode: str) -> io.BufferedReader:
        if mode != "rb":
            raise OSError("Bright Data response stream requires binary reads")
        return io.BufferedReader(_DeadlineSocketReader(self))

    def recv_into(self, buffer: Any) -> int:
        remaining = self.task_deadline - self.monotonic()
        if remaining <= 0:
            raise TimeoutError from None
        self.network_socket.settimeout(min(self.timeout_seconds, remaining))
        try:
            received = self.network_socket.recv_into(buffer)
        except socket.timeout:
            if self.monotonic() >= self.task_deadline:
                raise TimeoutError from None
            raise
        if self.monotonic() >= self.task_deadline:
            raise TimeoutError from None
        return received


class _DeadlineSocketReader(io.RawIOBase):
    """Raw stream whose every refill is bounded by the remaining task budget."""

    def __init__(self, network_socket: _DeadlineSocket):
        super().__init__()
        self.network_socket = network_socket

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        return self.network_socket.recv_into(buffer)


def _deadline_create_connection(address: tuple[str, int], *, timeout_seconds: float,
                                task_deadline: float, monotonic: Callable[[], float],
                                source_address: tuple[str, int] | None = None) -> Any:
    """Bound TCP attempts together after synchronous name resolution returns."""
    host, port = address
    try:
        addresses = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    except OSError:
        if monotonic() >= task_deadline:
            raise TimeoutError from None
        raise
    if monotonic() >= task_deadline:
        raise TimeoutError from None

    last_error: OSError | None = None
    for family, socktype, proto, _canonname, sockaddr in addresses:
        network_socket = None
        try:
            remaining = task_deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError
            network_socket = socket.socket(family, socktype, proto)
            if source_address:
                network_socket.bind(source_address)
            network_socket.settimeout(min(timeout_seconds, remaining))
            network_socket.connect(sockaddr)
            if monotonic() >= task_deadline:
                raise TimeoutError
            return network_socket
        except OSError as exc:
            if network_socket is not None:
                network_socket.close()
            if monotonic() >= task_deadline:
                raise TimeoutError from None
            last_error = exc
    if last_error is not None:
        raise last_error
    raise OSError("getaddrinfo returns an empty list")


class _DeadlineTLSContext:
    """Refresh the HTTPS socket timeout immediately before the TLS handshake."""

    def __init__(self, context: Any, timeout_seconds: float,
                 task_deadline: float, monotonic: Callable[[], float]):
        self.context = context
        self.timeout_seconds = timeout_seconds
        self.task_deadline = task_deadline
        self.monotonic = monotonic

    def wrap_socket(self, network_socket: Any, *args: Any, **kwargs: Any) -> Any:
        remaining = self.task_deadline - self.monotonic()
        if remaining <= 0:
            raise TimeoutError from None
        network_socket.settimeout(min(self.timeout_seconds, remaining))
        try:
            wrapped_socket = self.context.wrap_socket(network_socket, *args, **kwargs)
        except (TimeoutError, socket.timeout):
            if self.monotonic() >= self.task_deadline:
                raise TimeoutError from None
            raise
        if self.monotonic() >= self.task_deadline:
            wrapped_socket.close()
            raise TimeoutError from None
        return wrapped_socket


def _apply_https_task_deadline(connection: Any, *, timeout_seconds: float,
                               task_deadline: float, monotonic: Callable[[], float]) -> None:
    if not isinstance(connection, _HTTPS_CONNECTION_TYPE):
        return
    connection._create_connection = lambda address, _timeout, source_address: _deadline_create_connection(
        address, timeout_seconds=timeout_seconds, task_deadline=task_deadline,
        monotonic=monotonic, source_address=source_address,
    )
    connection._context = _DeadlineTLSContext(
        connection._context, timeout_seconds, task_deadline, monotonic,
    )


class BrightDataHTTPTransport:
    """HTTP transport that applies one monotonic deadline to the full exchange."""

    def __init__(self, *, timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
                 monotonic: Callable[[], float] = time.monotonic):
        try:
            timeout = float(timeout_seconds)
        except (TypeError, ValueError):
            raise BrightDataConfigurationError("Bright Data HTTP timeout must be a positive finite number") from None
        if not math.isfinite(timeout) or timeout <= 0:
            raise BrightDataConfigurationError("Bright Data HTTP timeout must be a positive finite number")
        self.timeout_seconds = timeout
        self.monotonic = monotonic

    def request(self, method: str, path: str, *, params: Mapping[str, Any],
                json_body: Any, token: str, timeout_seconds: float,
                task_deadline: float, on_attempt: Callable[[], None]) -> BrightDataHTTPResponse:
        query = urllib.parse.urlencode({key: _query_value(value) for key, value in params.items()})
        url = f"{API_ROOT}{path}" + (f"?{query}" if query else "")
        data = None if json_body is None else json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        current_method = method
        current_data = data
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        redirects = 0

        while True:
            remaining = task_deadline - self.monotonic()
            if remaining <= 0:
                raise TimeoutError from None
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise OSError("Bright Data redirect URL is invalid") from None
            socket_timeout = min(self.timeout_seconds, timeout_seconds)
            request_timeout = min(socket_timeout, remaining)
            on_attempt()
            connection_type = (http.client.HTTPSConnection if parsed.scheme == "https"
                               else http.client.HTTPConnection)
            connection = connection_type(parsed.hostname, parsed.port, timeout=request_timeout)
            if parsed.scheme == "https":
                _apply_https_task_deadline(
                    connection, timeout_seconds=socket_timeout,
                    task_deadline=task_deadline, monotonic=self.monotonic,
                )
            http_response = None
            try:
                connection.connect()
                remaining = task_deadline - self.monotonic()
                if remaining <= 0:
                    raise TimeoutError from None
                connection.sock.settimeout(min(self.timeout_seconds, timeout_seconds, remaining))
                request_target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
                connection.request(current_method, request_target, body=current_data, headers=headers)
                if self.monotonic() >= task_deadline:
                    raise TimeoutError from None

                deadline_socket = _DeadlineSocket(
                    connection.sock, min(self.timeout_seconds, timeout_seconds),
                    task_deadline, self.monotonic,
                )
                http_response = http.client.HTTPResponse(deadline_socket, method=current_method)
                http_response.begin()
                if self.monotonic() >= task_deadline:
                    raise TimeoutError from None

                location = http_response.getheader("Location")
                if http_response.status in {301, 302, 303, 307, 308} and location and redirects < 10:
                    next_url = urllib.parse.urljoin(url, location)
                    next_parsed = urllib.parse.urlsplit(next_url)
                    if next_parsed.scheme not in {"http", "https"} or not next_parsed.hostname \
                            or next_parsed.username or next_parsed.password:
                        raise OSError("Bright Data redirect URL is invalid") from None
                    if _url_origin(parsed) != _url_origin(next_parsed):
                        headers = {key: value for key, value in headers.items()
                                   if key.casefold() != "authorization"}
                    if http_response.status in {301, 302, 303} and current_method.upper() == "POST":
                        current_method = "GET"
                        current_data = None
                        headers = {key: value for key, value in headers.items()
                                   if key.casefold() not in {"content-type", "content-length", "transfer-encoding"}}
                    url = next_url
                    redirects += 1
                    continue

                raw = http_response.read()
                payload = _decode_payload(raw)
                if self.monotonic() >= task_deadline:
                    raise TimeoutError from None
                return BrightDataHTTPResponse(
                    http_response.status, payload, dict(http_response.getheaders()),
                )
            except (TimeoutError, socket.timeout):
                if self.monotonic() >= task_deadline:
                    raise TimeoutError from None
                raise
            except Exception:
                if self.monotonic() >= task_deadline:
                    raise TimeoutError from None
                raise
            finally:
                if http_response is not None:
                    http_response.close()
                connection.close()


def _url_origin(parsed: urllib.parse.SplitResult) -> tuple[str, str, int | None]:
    scheme = parsed.scheme.casefold()
    port = parsed.port
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, (parsed.hostname or "").casefold(), port


@dataclass(frozen=True)
class BrightDataRuntimeConfig:
    token: str = field(repr=False)
    platforms: Mapping[str, BrightDataPlatformConfig] = field(default_factory=dict)
    task_timeout_seconds: float = DEFAULT_TASK_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if isinstance(self.task_timeout_seconds, bool):
            raise BrightDataConfigurationError("Bright Data task timeout must be a positive finite number of seconds")
        try:
            timeout = float(self.task_timeout_seconds)
        except (TypeError, ValueError):
            raise BrightDataConfigurationError("Bright Data task timeout must be a positive finite number of seconds") from None
        if not math.isfinite(timeout) or timeout <= 0:
            raise BrightDataConfigurationError("Bright Data task timeout must be a positive finite number of seconds")
        object.__setattr__(self, "task_timeout_seconds", timeout)


@dataclass(frozen=True)
class _TaskDeadline:
    started_at: float
    budget_seconds: float
    monotonic: Callable[[], float]

    @property
    def expires_at(self) -> float:
        return self.started_at + self.budget_seconds

    def remaining(self) -> float:
        return self.expires_at - self.monotonic()

    def diagnostics(self, requests_submitted: int) -> dict[str, Any]:
        return {
            "task_timeout_seconds": self.budget_seconds,
            "task_elapsed_seconds": round(max(0.0, self.monotonic() - self.started_at), 3),
            "requests_submitted": requests_submitted,
        }


def _query_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _decode_payload(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw.decode("utf-8", errors="replace")


def _runtime_task_timeout_seconds(environ: Mapping[str, str]) -> float:
    raw = environ.get(TASK_TIMEOUT_ENV, str(DEFAULT_TASK_TIMEOUT_SECONDS))
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        raise BrightDataConfigurationError(
            "Bright Data task timeout must be a positive finite number of seconds",
        ) from None
    if not math.isfinite(timeout) or timeout <= 0:
        raise BrightDataConfigurationError(
            "Bright Data task timeout must be a positive finite number of seconds",
        )
    return timeout


def _safe_field(raw: Any, where: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise BrightDataConfigurationError(f"{where} must name a configured schema field")
    return raw.strip()


def _parse_input_field(raw: Any, semantic: str, platform: str) -> BrightDataField:
    location = f"{platform} input_schema.{semantic}"
    if semantic == "keyword":
        return BrightDataField(field=_safe_field(raw, location))
    if not isinstance(raw, Mapping):
        raise BrightDataConfigurationError(f"{location} must be an object with an exact field mapping")
    field_name = _safe_field(raw.get("field"), f"{location}.field")
    if semantic == "location":
        if set(raw) - {"field", "source", "value"}:
            raise BrightDataConfigurationError(f"{location} contains unsupported schema options")
        source = str(raw.get("source") or "").strip()
        value = raw.get("value")
        if (source not in {"home_state", "home_metro"}) == (value is None):
            raise BrightDataConfigurationError(f"{location} needs exactly one supported source or literal value")
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise BrightDataConfigurationError(f"{location}.value must be a JSON scalar")
        return BrightDataField(field=field_name, source=source, value=value)
    if semantic == "remote":
        if set(raw) - {"field", "value"}:
            raise BrightDataConfigurationError(f"{location} contains unsupported schema options")
        value = raw.get("value")
        if value is None or not isinstance(value, (str, int, float, bool)):
            raise BrightDataConfigurationError(f"{location}.value must match the configured scraper schema")
        return BrightDataField(field=field_name, value=value)
    if set(raw) - {"field", "template", "type"}:
        raise BrightDataConfigurationError(f"{location} contains unsupported schema options")
    template = raw.get("template")
    kind = str(raw.get("type") or "").strip().casefold()
    if template is not None:
        if not isinstance(template, str) or "{days}" not in template:
            raise BrightDataConfigurationError(f"{location}.template must explicitly map the task day window")
        try:
            template.format(days=1)
        except (KeyError, IndexError, ValueError) as exc:
            raise BrightDataConfigurationError(f"{location}.template contains an unsupported placeholder") from exc
        return BrightDataField(field=field_name, template=template)
    if kind == "integer":
        return BrightDataField(field=field_name, kind="integer")
    raise BrightDataConfigurationError(f"{location} needs an explicit template or type='integer'")


def parse_platform_config(platform: str, raw: str) -> BrightDataPlatformConfig:
    if platform not in PLATFORM_ENV:
        raise BrightDataConfigurationError("unsupported Bright Data source surface")
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise BrightDataConfigurationError(f"{PLATFORM_ENV[platform]} must contain valid JSON") from exc
    if not isinstance(value, Mapping):
        raise BrightDataConfigurationError(f"{PLATFORM_ENV[platform]} must contain a JSON object")
    if set(value) - {"dataset_id", "input_schema", "output_schema", "result_rows_key"}:
        raise BrightDataConfigurationError(f"{PLATFORM_ENV[platform]} has unsupported configuration keys")
    dataset_id = value.get("dataset_id")
    if not isinstance(dataset_id, str) or not dataset_id.strip():
        raise BrightDataConfigurationError(f"{PLATFORM_ENV[platform]} needs a non-empty dataset_id")
    schema = value.get("input_schema")
    if not isinstance(schema, Mapping) or "keyword" not in schema:
        raise BrightDataConfigurationError(f"{PLATFORM_ENV[platform]} needs input_schema.keyword")
    unknown_input = set(schema) - {"keyword", "location", "remote", "window_days"}
    if unknown_input:
        raise BrightDataConfigurationError(f"{PLATFORM_ENV[platform]} input_schema has unsupported semantic keys")
    keyword = _parse_input_field(schema["keyword"], "keyword", platform).field
    filters = {
        semantic: _parse_input_field(schema[semantic], semantic, platform)
        for semantic in ("location", "remote", "window_days") if semantic in schema
    }
    input_fields = [keyword, *(item.field for item in filters.values())]
    if len(input_fields) != len(set(input_fields)):
        raise BrightDataConfigurationError(f"{PLATFORM_ENV[platform]} maps two task fields to the same input field")
    output = value.get("output_schema")
    if not isinstance(output, Mapping):
        raise BrightDataConfigurationError(f"{PLATFORM_ENV[platform]} needs the exact output_schema field map")
    if set(output) - SUPPORTED_OUTPUT_FIELDS:
        raise BrightDataConfigurationError(f"{PLATFORM_ENV[platform]} output_schema has unsupported semantic keys")
    output_schema: dict[str, str] = {}
    for semantic, field_name in output.items():
        output_schema[str(semantic)] = _safe_field(field_name, f"{platform} output_schema.{semantic}")
    if len(set(output_schema.values())) != len(output_schema):
        raise BrightDataConfigurationError(f"{PLATFORM_ENV[platform]} maps two observations to the same output field")
    if not (output_schema.get("source_job_id") or output_schema.get("source_url")):
        raise BrightDataConfigurationError(f"{PLATFORM_ENV[platform]} output_schema needs source_job_id or source_url")
    rows_key = value.get("result_rows_key", "")
    if not isinstance(rows_key, str):
        raise BrightDataConfigurationError(f"{PLATFORM_ENV[platform]} result_rows_key must be a string")
    return BrightDataPlatformConfig(
        platform=platform, dataset_id=dataset_id.strip(), keyword_field=keyword,
        location=filters.get("location"), remote=filters.get("remote"),
        window_days=filters.get("window_days"), output_schema=output_schema,
        result_rows_key=rows_key.strip(),
    )


def brightdata_preflight(platforms: list[str], environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Offline presence/schema check; never verifies a credential with the network."""
    env = os.environ if environ is None else environ
    token_present = bool(str(env.get(TOKEN_ENV, "")).strip())
    try:
        _runtime_task_timeout_seconds(env)
    except BrightDataConfigurationError:
        task_timeout_valid = False
    else:
        task_timeout_valid = True
    configured: dict[str, dict[str, bool]] = {}
    for platform in platforms:
        key = PLATFORM_ENV.get(platform)
        raw = str(env.get(key, "")) if key else ""
        try:
            if not raw:
                raise BrightDataConfigurationError("missing")
            parse_platform_config(platform, raw)
        except BrightDataConfigurationError:
            configured[platform] = {"present": bool(raw), "valid": False}
        else:
            configured[platform] = {"present": True, "valid": True}
    return {
        "provider": "brightdata-jobs", "token_present": token_present,
        "token_validity": "not_checked_offline",
        "task_timeout_valid": task_timeout_valid, "platform_configs": configured,
        "valid": token_present and task_timeout_valid and all(value["valid"] for value in configured.values()),
    }


def brightdata_runtime_config(platforms: list[str], *, environ: Mapping[str, str] | None = None) -> BrightDataRuntimeConfig:
    env = os.environ if environ is None else environ
    token = str(env.get(TOKEN_ENV, "")).strip()
    if not token:
        raise BrightDataConfigurationError(f"{TOKEN_ENV} is required")
    configs: dict[str, BrightDataPlatformConfig] = {}
    for platform in platforms:
        variable = PLATFORM_ENV.get(platform)
        if not variable:
            raise BrightDataConfigurationError("unsupported Bright Data source surface")
        raw = str(env.get(variable, ""))
        if not raw:
            raise BrightDataConfigurationError(f"{variable} is required for the selected source surface")
        configs[platform] = parse_platform_config(platform, raw)
    return BrightDataRuntimeConfig(
        token=token, platforms=configs, task_timeout_seconds=_runtime_task_timeout_seconds(env),
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _scalar(value: Any) -> str:
    if value is None or isinstance(value, (dict, list, tuple, bool)):
        return ""
    return str(value).strip()


def _sanitize(value: Any, *, token: str = "") -> Any:
    if isinstance(value, Mapping):
        clean: dict[str, Any] = {}
        for key, child in value.items():
            raw_key = str(key)
            normalized_key = raw_key.casefold().replace("-", "_")
            if token and token in raw_key:
                continue
            if normalized_key in TRUST_CLAIM_FIELDS or any(part in normalized_key for part in SECRET_FIELD_PARTS):
                continue
            safe_child = _sanitize(child, token=token)
            if safe_child is not None:
                clean[raw_key] = safe_child
        return clean
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, token=token) for item in value]
    if isinstance(value, str) and token:
        return value.replace(token, "[REDACTED]")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _contains_token(value: Any, token: str) -> bool:
    if not token:
        return False
    if isinstance(value, Mapping):
        return any(token in str(key) or _contains_token(child, token) for key, child in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_token(item, token) for item in value)
    return isinstance(value, str) and token in value


def _row_page(payload: Any, rows_key: str) -> tuple[list[Mapping[str, Any]], Mapping[str, Any]]:
    if isinstance(payload, list):
        rows = payload
        envelope: Mapping[str, Any] = {}
    elif isinstance(payload, Mapping) and rows_key:
        rows = payload.get(rows_key)
        envelope = payload
    else:
        raise ValueError("snapshot result must be a row array or use the configured result_rows_key")
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise ValueError("snapshot rows must be an array of objects")
    return list(rows), envelope


def _continuation(envelope: Mapping[str, Any]) -> tuple[bool, dict[str, Any]]:
    markers: dict[str, Any] = {}
    for key in ("truncated", "has_more", "limit_reached"):
        if key in envelope and envelope[key] not in (None, False, 0, "", "false"):
            markers[key] = envelope[key]
    for key in ("next_cursor", "next_page", "next_part", "cursor"):
        if key in envelope and envelope[key] not in (None, "", False):
            markers[key] = envelope[key]
    return bool(markers), markers


def _reported_cost(payload: Any, *, token: str = "") -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    returned = {}
    for key, value in payload.items():
        normalized = str(key).casefold()
        if value is not None and isinstance(value, (str, int, float, list, dict)) and (
            "cost" in normalized or "credit" in normalized
        ):
            returned[str(key)] = value
    safe = _sanitize(returned, token=token)
    return dict(safe) if isinstance(safe, Mapping) else {}


def _status_failure(status: str, error: str = "") -> tuple[ProviderFailureClass, str]:
    normalized = f"{status} {error}".casefold()
    if "auth" in normalized or "suspend" in normalized:
        return ProviderFailureClass.AUTHORIZATION, "Bright Data rejected authorization or account access"
    if "input" in normalized or "schema" in normalized or "validation" in normalized:
        return ProviderFailureClass.SCHEMA, "Bright Data reported an input or schema failure"
    return ProviderFailureClass.UNKNOWN, "Bright Data collection ended in a failed or canceled state"


class BrightDataJobsProvider:
    """Bright Data Jobs Scraper adapter behind the acquisition-v2 seam."""

    name = "brightdata-jobs"

    def __init__(
        self, runtime: BrightDataRuntimeConfig, *, candidate: Mapping[str, Any], max_records: int,
        transport: BrightDataTransport | None = None, run_id: str = "", max_polls: int = 20,
        max_transient_retries: int = 2, poll_interval_seconds: float = 5,
        sleep: Callable[[float], None] = time.sleep, batch_size: int = 1000,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        if int(max_records) < 1:
            raise BrightDataConfigurationError("max_records must be a positive explicit validation budget")
        if int(max_polls) < 1 or int(max_transient_retries) < 0:
            raise BrightDataConfigurationError("Bright Data poll/retry bounds are invalid")
        if int(batch_size) < 1000:
            raise BrightDataConfigurationError("Bright Data batch_size must follow the documented minimum of 1000")
        self._token = runtime.token
        self.platforms = dict(runtime.platforms)
        self.candidate = dict(candidate)
        self.max_records = int(max_records)
        self.monotonic = monotonic
        self.transport = transport or BrightDataHTTPTransport(monotonic=monotonic)
        transport_timeout = getattr(self.transport, "timeout_seconds", DEFAULT_HTTP_TIMEOUT_SECONDS)
        try:
            transport_timeout = float(transport_timeout)
        except (TypeError, ValueError):
            raise BrightDataConfigurationError("Bright Data HTTP timeout must be a positive finite number") from None
        if not math.isfinite(transport_timeout) or transport_timeout <= 0:
            raise BrightDataConfigurationError("Bright Data HTTP timeout must be a positive finite number")
        self.socket_timeout_seconds = min(DEFAULT_HTTP_TIMEOUT_SECONDS, transport_timeout)
        self.task_timeout_seconds = runtime.task_timeout_seconds
        self.run_id = run_id or f"brightdata:{uuid.uuid4()}"
        self.max_polls = int(max_polls)
        self.max_transient_retries = int(max_transient_retries)
        self.poll_interval_seconds = max(0, float(poll_interval_seconds))
        self.sleep = sleep
        self.batch_size = int(batch_size)
        self._records_delivered_total = 0

    @property
    def records_delivered_total(self) -> int:
        return self._records_delivered_total

    def fetch(self, task: SearchTask) -> ProviderBatch:
        deadline = _TaskDeadline(self.monotonic(), self.task_timeout_seconds, self.monotonic)
        requests_submitted = 0
        config = self.platforms.get(task.platform)
        if config is None:
            raise ProviderFailure(ProviderFailureClass.CONFIGURATION,
                                  "Bright Data platform configuration is missing", retryable=False,
                                  provider_metadata=deadline.diagnostics(requests_submitted))
        remaining = self.max_records - self._records_delivered_total
        task_provenance = {
            "task_key": task.task_key, "query_text": task.query, "window_days": task.age_days,
            "platform": task.platform, "query_family": task.query_family, "query_kind": task.query_kind,
            "query_pass": task.query_pass, "phase": task.phase, "execution_rank": task.execution_rank,
        }
        if remaining <= 0:
            metadata = {"task_provenance": task_provenance, "budget_stop": True,
                        "records_budget": self.max_records, "records_delivered_total": self._records_delivered_total,
                        **deadline.diagnostics(requests_submitted)}
            return ProviderBatch(
                completion_state=ProviderCompletionState.INCOMPLETE,
                failure_class=ProviderFailureClass.PARTIAL_BATCH,
                provider_task_id="", provider_metadata=metadata,
            )
        try:
            input_row = self._input_row(task, config)
        except ProviderFailure as exc:
            exc.provider_metadata.update(deadline.diagnostics(requests_submitted))
            raise
        trigger_params: dict[str, Any] = {
            "dataset_id": config.dataset_id, "type": "discover_new", "discover_by": "keyword",
            "include_errors": True, "format": "json", "limit_multiple_results": remaining,
        }
        trigger_shape = {"method": "POST", "path": "/datasets/v3/trigger",
                         "query": dict(trigger_params), "body": [dict(input_row)]}
        if _contains_token(trigger_shape, self._token):
            raise ProviderFailure(ProviderFailureClass.CONFIGURATION,
                                  "Bright Data request configuration contains a secret value", retryable=False,
                                  provider_metadata=deadline.diagnostics(requests_submitted))
        fingerprint = hashlib.sha256(_canonical_json(trigger_shape).encode("utf-8")).hexdigest()
        shape: dict[str, Any] = {
            "trigger": trigger_shape,
            "progress_checks": [], "parts_request": None, "result_requests": [],
        }
        provider_task_id = ""
        actual_records = 0
        reported_cost: dict[str, Any] = {}
        base_metadata = {
            "task_provenance": task_provenance, "request_shape": shape,
            "request_fingerprint": fingerprint, "records_budget": self.max_records,
            "records_delivered_before_task": self._records_delivered_total,
            **deadline.diagnostics(requests_submitted),
        }

        def fail(classification: ProviderFailureClass, message: str,
                 metadata: Mapping[str, Any], records_delivered: int,
                 cost: Mapping[str, Any], *, retryable: bool = True) -> ProviderFailure:
            diagnostics = deadline.diagnostics(requests_submitted)
            base_metadata.update(diagnostics)
            failure_metadata = dict(metadata)
            failure_metadata.update(diagnostics)
            return self._failure(
                classification, message, failure_metadata, requests_submitted,
                records_delivered, cost, retryable=retryable,
            )

        def request(method: str, path: str, params: Mapping[str, Any], json_body: Any) -> BrightDataHTTPResponse:
            nonlocal requests_submitted
            remaining_seconds = deadline.remaining()
            if remaining_seconds <= 0:
                raise TimeoutError from None

            def record_attempt() -> None:
                nonlocal requests_submitted
                if deadline.remaining() <= 0:
                    raise TimeoutError from None
                requests_submitted += 1

            response = self.transport.request(
                method, path, params=params, json_body=json_body, token=self._token,
                timeout_seconds=min(self.socket_timeout_seconds, remaining_seconds),
                task_deadline=deadline.expires_at, on_attempt=record_attempt,
            )
            if deadline.remaining() <= 0:
                raise TimeoutError from None
            return response

        def bounded_sleep(seconds: float) -> None:
            remaining_seconds = deadline.remaining()
            if remaining_seconds <= 0:
                raise TimeoutError from None
            duration = min(max(0.0, seconds), remaining_seconds)
            consumes_budget = duration >= remaining_seconds
            if duration:
                self.sleep(duration)
            if consumes_budget or deadline.remaining() <= 0:
                raise TimeoutError from None

        try:
            triggered = request("POST", trigger_shape["path"], trigger_params, [dict(input_row)])
        except (TimeoutError, socket.timeout):
            raise fail(ProviderFailureClass.TIMEOUT, "Bright Data trigger timed out",
                       base_metadata, 0, reported_cost) from None
        except Exception as exc:
            raise fail(ProviderFailureClass.TRANSPORT,
                       f"Bright Data trigger transport failed ({type(exc).__name__})",
                       base_metadata, 0, reported_cost) from None
        reported_cost.update(_reported_cost(triggered.payload, token=self._token))
        if triggered.status in (401, 403):
            raise fail(ProviderFailureClass.AUTHORIZATION,
                       f"Bright Data trigger returned HTTP {triggered.status}",
                       base_metadata, 0, reported_cost, retryable=False)
        if triggered.status in (429,) or 500 <= triggered.status <= 599:
            raise fail(ProviderFailureClass.TRANSPORT,
                       f"Bright Data trigger returned transient HTTP {triggered.status}",
                       base_metadata, 0, reported_cost)
        if triggered.status in (400, 422):
            raise fail(ProviderFailureClass.SCHEMA,
                       f"Bright Data rejected configured input with HTTP {triggered.status}",
                       base_metadata, 0, reported_cost, retryable=False)
        if triggered.status == 404:
            raise fail(ProviderFailureClass.CONFIGURATION,
                       "Bright Data dataset configuration was not found (HTTP 404)",
                       base_metadata, 0, reported_cost, retryable=False)
        if triggered.status not in (200, 202) or not isinstance(triggered.payload, Mapping):
            raise fail(ProviderFailureClass.INVALID_RESPONSE,
                       "Bright Data trigger returned an invalid acknowledgement",
                       base_metadata, 0, reported_cost, retryable=False)
        provider_task_id = _scalar(triggered.payload.get("snapshot_id"))
        if not provider_task_id or self._token in provider_task_id:
            raise fail(ProviderFailureClass.INVALID_RESPONSE,
                       "Bright Data trigger acknowledgement omitted a safe snapshot_id",
                       base_metadata, 0, reported_cost, retryable=False)
        base_metadata["snapshot_id"] = provider_task_id

        for poll_index in range(self.max_polls):
            if poll_index:
                try:
                    bounded_sleep(self.poll_interval_seconds)
                except (TimeoutError, socket.timeout):
                    raise fail(ProviderFailureClass.TIMEOUT,
                               "Bright Data task deadline expired during readiness polling",
                               base_metadata, actual_records, reported_cost) from None
            progress_path = f"/datasets/v3/progress/{urllib.parse.quote(provider_task_id, safe='')}"
            progress_request = {"method": "GET", "path": progress_path, "query": {}}
            shape["progress_checks"].append(progress_request)
            progress = self._get(progress_path, {}, base_metadata, actual_records,
                                 reported_cost, "progress", deadline=deadline, request=request, fail=fail)
            reported_cost.update(_reported_cost(progress.payload, token=self._token))
            if progress.status != 200 or not isinstance(progress.payload, Mapping):
                raise fail(ProviderFailureClass.INVALID_RESPONSE,
                           "Bright Data progress response was malformed",
                           base_metadata, actual_records, reported_cost, retryable=False)
            progress_status = str(progress.payload.get("status") or "").casefold()
            if progress_status in {"starting", "running"}:
                continue
            if progress_status in {"failed", "canceled"}:
                classification, message = _status_failure(
                    progress_status, _scalar(progress.payload.get("error_message")),
                )
                reported_cost.update(_reported_cost(progress.payload, token=self._token))
                raise fail(classification, message, base_metadata, actual_records,
                           reported_cost, retryable=False)
            if progress_status != "ready":
                raise fail(ProviderFailureClass.INVALID_RESPONSE,
                           "Bright Data progress returned an unknown terminal state",
                           base_metadata, actual_records, reported_cost, retryable=False)

            parts_path = f"/datasets/v3/snapshot/{urllib.parse.quote(provider_task_id, safe='')}/parts"
            parts_params = {"batch_size": self.batch_size}
            shape["parts_request"] = {"method": "GET", "path": parts_path, "query": dict(parts_params)}
            parts_response = self._get(parts_path, parts_params, base_metadata, actual_records,
                                       reported_cost, "parts", deadline=deadline, request=request, fail=fail)
            reported_cost.update(_reported_cost(parts_response.payload, token=self._token))
            if parts_response.status != 200 or not isinstance(parts_response.payload, Mapping):
                raise fail(ProviderFailureClass.AMBIGUOUS_CURSOR,
                           "Bright Data did not provide snapshot part-completion evidence",
                           base_metadata, actual_records, reported_cost)
            parts_total = parts_response.payload.get("parts")
            if isinstance(parts_total, bool) or not isinstance(parts_total, int) or parts_total < 1:
                raise fail(ProviderFailureClass.AMBIGUOUS_CURSOR,
                           "Bright Data snapshot part count was missing or invalid",
                           base_metadata, actual_records, reported_cost)
            base_metadata["parts_total"] = parts_total
            mapped_records: list[AcquisitionRecord] = []
            part_numbers: list[int] = []
            continuation: dict[str, Any] = {}
            invalid_rows = 0
            provider_errors = 0
            remaining_for_task = max(0, remaining)

            def partial_result_failure(failure: ProviderFailure) -> ProviderBatch:
                base_metadata.update({
                    "records_delivered": actual_records,
                    "records_delivered_total": self._records_delivered_total,
                    "parts_retrieved": len(part_numbers), "part_numbers": list(part_numbers),
                    "provider_error_rows": provider_errors, "invalid_rows": invalid_rows,
                    "result_failure_class": failure.classification.value,
                    **deadline.diagnostics(requests_submitted),
                })
                return ProviderBatch(
                    records=tuple(mapped_records[:remaining_for_task]),
                    completion_state=(ProviderCompletionState.RETRYABLE if failure.retryable
                                      else ProviderCompletionState.INCOMPLETE),
                    failure_class=failure.classification, provider_task_id=provider_task_id,
                    provider_metadata=self._provider_metadata(
                        {**dict(failure.provider_metadata), **base_metadata}, shape, fingerprint, reported_cost,
                    ),
                    requests_submitted=requests_submitted, records_delivered=actual_records,
                    reported_cost=reported_cost,
                )

            for part in range(1, parts_total + 1):
                if self._records_delivered_total >= self.max_records:
                    break
                result_path = f"/datasets/v3/snapshot/{urllib.parse.quote(provider_task_id, safe='')}"
                result_params = {"format": "json", "batch_size": self.batch_size, "part": part}
                shape["result_requests"].append({"method": "GET", "path": result_path, "query": dict(result_params)})
                try:
                    result = self._get(result_path, result_params, base_metadata, actual_records,
                                       reported_cost, "result", deadline=deadline, request=request, fail=fail)
                except ProviderFailure as exc:
                    return partial_result_failure(exc)
                reported_cost.update(_reported_cost(result.payload, token=self._token))
                if deadline.remaining() <= 0:
                    return partial_result_failure(fail(
                        ProviderFailureClass.TIMEOUT,
                        "Bright Data task deadline expired during result retrieval",
                        base_metadata, actual_records, reported_cost,
                    ))
                if result.status in (202, 404, 409):
                    return partial_result_failure(fail(
                        ProviderFailureClass.PARTIAL_BATCH,
                        "Bright Data snapshot result part is not available",
                        base_metadata, actual_records, reported_cost,
                    ))
                if result.status == 200 and result.payload in (None, ""):
                    return partial_result_failure(fail(
                        ProviderFailureClass.PARTIAL_BATCH,
                        "Bright Data snapshot result part was empty or unavailable",
                        base_metadata, actual_records, reported_cost,
                    ))
                if result.status != 200:
                    return partial_result_failure(fail(
                        ProviderFailureClass.INVALID_RESPONSE,
                        "Bright Data snapshot result returned an invalid HTTP status",
                        base_metadata, actual_records, reported_cost,
                        retryable=False,
                    ))
                if isinstance(result.payload, Mapping) and str(result.payload.get("status") or "").casefold() in {
                    "starting", "running", "building",
                }:
                    return partial_result_failure(fail(
                        ProviderFailureClass.PARTIAL_BATCH,
                        "Bright Data snapshot result part is still pending",
                        base_metadata, actual_records, reported_cost,
                    ))
                try:
                    rows, envelope = _row_page(result.payload, config.result_rows_key)
                except ValueError as exc:
                    return partial_result_failure(fail(
                        ProviderFailureClass.INVALID_RESPONSE, str(exc), base_metadata,
                        actual_records, reported_cost, retryable=False,
                    ))
                more, marker = _continuation(envelope)
                if more:
                    continuation.update(marker)
                part_numbers.append(part)
                actual_records += len(rows)
                self._records_delivered_total += len(rows)
                for row in rows:
                    if row.get("error") not in (None, "", False, []):
                        provider_errors += 1
                        continue
                    if len(mapped_records) >= remaining_for_task:
                        continue
                    mapper = {
                        "linkedin": map_linkedin_row,
                        "indeed": map_indeed_row,
                        "glassdoor": map_glassdoor_row,
                    }[task.platform]
                    try:
                        mapped = mapper(self, task, config, row, provider_task_id, shape, fingerprint, part)
                    except ValueError:
                        invalid_rows += 1
                        continue
                    mapped_records.append(mapped)
                if deadline.remaining() <= 0:
                    return partial_result_failure(fail(
                        ProviderFailureClass.TIMEOUT,
                        "Bright Data task deadline expired during result processing",
                        base_metadata, actual_records, reported_cost,
                    ))
            actual_count = actual_records
            records_for_ingestion = mapped_records[:remaining_for_task]
            if len(mapped_records) > remaining_for_task:
                invalid_rows += len(mapped_records) - remaining_for_task
            base_metadata.update({
                "records_delivered": actual_count, "records_delivered_total": self._records_delivered_total,
                "parts_retrieved": len(part_numbers), "part_numbers": part_numbers,
                "provider_error_rows": provider_errors, "invalid_rows": invalid_rows,
            })
            if continuation:
                base_metadata["continuation_markers"] = _sanitize(continuation, token=self._token)
            capped = actual_count >= remaining
            parts_missing = len(part_numbers) < parts_total
            incomplete = bool(continuation or invalid_rows or provider_errors or capped or parts_missing)
            if deadline.remaining() <= 0:
                return partial_result_failure(fail(
                    ProviderFailureClass.TIMEOUT,
                    "Bright Data task deadline expired before task completion",
                    base_metadata, actual_records, reported_cost,
                ))
            evidence = {
                "platform": task.platform, "task_key": task.task_key,
                "provider_job_id": provider_task_id, "terminal_state": progress_status,
                "batch": {"parts_total": parts_total, "parts_retrieved": len(part_numbers),
                          "part_numbers": part_numbers, "records_delivered": actual_count,
                          "continuation": _sanitize(continuation, token=self._token)},
                "request_fingerprint": fingerprint,
            }
            base_metadata["completion_observation"] = evidence
            base_metadata.update(deadline.diagnostics(requests_submitted))
            failure_class = (
                ProviderFailureClass.INVALID_RESPONSE if invalid_rows or provider_errors
                else ProviderFailureClass.AMBIGUOUS_CURSOR if continuation or parts_missing
                else ProviderFailureClass.PARTIAL_BATCH if capped else None
            )
            return ProviderBatch(
                records=tuple(records_for_ingestion),
                completion_state=ProviderCompletionState.INCOMPLETE if incomplete else ProviderCompletionState.COMPLETE,
                completion_evidence=evidence if not incomplete else {},
                failure_class=failure_class,
                provider_task_id=provider_task_id,
                provider_metadata=self._provider_metadata(base_metadata, shape, fingerprint, reported_cost),
                requests_submitted=requests_submitted,
                records_delivered=actual_count,
                reported_cost=reported_cost,
            )

        raise fail(ProviderFailureClass.TIMEOUT,
                   "Bright Data collection did not reach terminal ready state within the poll bound",
                   base_metadata, actual_records, reported_cost)

    def _input_row(self, task: SearchTask, config: BrightDataPlatformConfig) -> dict[str, Any]:
        row: dict[str, Any] = {config.keyword_field: task.query}
        if config.location is not None:
            if config.location.source == "home_state":
                location_value = self.candidate.get("state", "")
            elif config.location.source == "home_metro":
                location_value = self.candidate.get("metro", "")
            else:
                location_value = config.location.value
            if location_value not in (None, ""):
                row[config.location.field] = location_value
        if config.remote is not None and task.remote_required:
            row[config.remote.field] = config.remote.value
        if config.window_days is not None:
            if config.window_days.kind == "integer":
                row[config.window_days.field] = task.age_days
            else:
                try:
                    row[config.window_days.field] = config.window_days.template.format(days=task.age_days)
                except (KeyError, IndexError, ValueError):
                    raise ProviderFailure(ProviderFailureClass.CONFIGURATION,
                                          "Bright Data freshness mapping is invalid", retryable=False) from None
        return row

    def _map_row(self, task: SearchTask, config: BrightDataPlatformConfig,
                 row: Mapping[str, Any], snapshot_id: str, request_shape: Mapping[str, Any],
                 fingerprint: str, part: int, *, token: str) -> AcquisitionRecord:
        if _contains_token(row, token):
            raise ValueError("provider row contains a secret value")
        values = {semantic: _scalar(row.get(source)) for semantic, source in config.output_schema.items()}
        source_job_id = values.get("source_job_id", "")
        source_url = values.get("source_url", "")
        if not (source_job_id or source_url):
            raise ValueError("provider row lacks configured source identity")
        urls: dict[str, str] = {}
        if source_url:
            urls.update({"discovery_url": source_url, "board_detail_url": source_url})
        for semantic, role in (
            ("application_url", "observed_board_apply_url"),
            ("employer_job_url", "employer_job_url"),
            ("ats_requisition_url", "ats_requisition_url"),
        ):
            if values.get(semantic):
                urls[role] = values[semantic]
        card: dict[str, Any] = {}
        for semantic, key in (
            ("title", "title"), ("company", "company"), ("location", "location"),
            ("posted_date", "posted_date"), ("posted_text", "posted_text"),
            ("employment_type", "employment_type"), ("salary", "salary_text"),
            ("summary", "summary"),
        ):
            if values.get(semantic):
                card[key] = values[semantic]
        detail: dict[str, Any] = {}
        for semantic, key in (
            ("title", "title"), ("company", "company"), ("location", "location"),
            ("posted_date", "posted_at"), ("employment_type", "employment_type"),
            ("salary", "salary_text"), ("description", "description"),
            ("summary", "summary"), ("application_url", "apply_url"),
            ("employer_job_url", "employer_job_url"), ("ats_requisition_url", "ats_requisition_url"),
        ):
            if values.get(semantic):
                detail[key] = values[semantic]
        if not detail:
            detail = None
        safe_row = _sanitize(row, token=token)
        raw_metadata = {
            "provider_row": safe_row,
            "provider_job_id": snapshot_id,
            "provider_batch": {"part": part},
            "request_fingerprint": fingerprint,
            "request_shape": _sanitize(request_shape, token=token),
        }
        return AcquisitionRecord(
            source_surface=task.platform, source_job_id=source_job_id,
            source_urls=urls, query_task_key=task.task_key,
            provider_record_id=values.get("provider_record_id", ""),
            observed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            card=card, detail=detail, raw_metadata=raw_metadata,
        )

    def _get(self, path: str, params: Mapping[str, Any], metadata: Mapping[str, Any],
             records_delivered: int, reported_cost: Mapping[str, Any], operation: str, *,
             deadline: _TaskDeadline, request: Callable[..., BrightDataHTTPResponse],
             fail: Callable[..., ProviderFailure]) -> BrightDataHTTPResponse:
        last_error = ""
        for attempt in range(self.max_transient_retries + 1):
            if deadline.remaining() <= 0:
                raise fail(ProviderFailureClass.TIMEOUT,
                           f"Bright Data task deadline expired during {operation}",
                           metadata, records_delivered, reported_cost)
            try:
                response = request("GET", path, params, None)
            except (TimeoutError, socket.timeout):
                if deadline.remaining() <= 0:
                    raise fail(ProviderFailureClass.TIMEOUT,
                               f"Bright Data task deadline expired during {operation}",
                               metadata, records_delivered, reported_cost) from None
                last_error = "timeout"
                if attempt < self.max_transient_retries:
                    continue
                raise fail(ProviderFailureClass.TIMEOUT,
                           f"Bright Data {operation} request timed out", metadata,
                           records_delivered, reported_cost) from None
            except Exception as exc:
                if deadline.remaining() <= 0:
                    raise fail(ProviderFailureClass.TIMEOUT,
                               f"Bright Data task deadline expired during {operation}",
                               metadata, records_delivered, reported_cost) from None
                last_error = type(exc).__name__
                if attempt < self.max_transient_retries:
                    continue
                raise fail(ProviderFailureClass.TRANSPORT,
                           f"Bright Data {operation} transport failed ({last_error})", metadata,
                           records_delivered, reported_cost) from None
            if response.status in (429,) or 500 <= response.status <= 599:
                if attempt < self.max_transient_retries:
                    continue
                raise fail(ProviderFailureClass.TRANSPORT,
                           f"Bright Data {operation} returned transient HTTP {response.status}", metadata,
                           records_delivered, reported_cost)
            if response.status in (401, 403):
                raise fail(ProviderFailureClass.AUTHORIZATION,
                           f"Bright Data {operation} returned HTTP {response.status}", metadata,
                           records_delivered, reported_cost, retryable=False)
            if response.status == 404 and operation in {"progress", "parts"}:
                raise fail(ProviderFailureClass.CONFIGURATION,
                           f"Bright Data {operation} endpoint returned HTTP 404", metadata,
                           records_delivered, reported_cost, retryable=False)
            return response
        raise fail(ProviderFailureClass.TRANSPORT,
                   f"Bright Data {operation} request failed ({last_error})", metadata,
                   records_delivered, reported_cost)

    def _failure(self, classification: ProviderFailureClass, message: str,
                 base_metadata: Mapping[str, Any], requests_submitted: int,
                 records_delivered: int, reported_cost: Mapping[str, Any], *,
                 retryable: bool = True) -> ProviderFailure:
        metadata_source = dict(base_metadata)
        metadata_source["records_delivered_partial"] = max(0, int(records_delivered))
        metadata_source["records_delivered_total"] = self._records_delivered_total
        metadata = self._provider_metadata(metadata_source, base_metadata.get("request_shape", {}),
                                           str(base_metadata.get("request_fingerprint") or ""), reported_cost)
        return ProviderFailure(
            classification, message, retryable=retryable, provider_metadata=metadata,
            requests_submitted=requests_submitted, records_delivered=records_delivered,
            reported_cost=reported_cost,
        )

    def _provider_metadata(self, metadata: Mapping[str, Any], request_shape: Mapping[str, Any],
                           fingerprint: str, reported_cost: Mapping[str, Any]) -> dict[str, Any]:
        output = dict(metadata)
        output["request_shape"] = _sanitize(request_shape, token=self._token)
        if fingerprint:
            output["request_fingerprint"] = fingerprint
        if reported_cost:
            output["provider_reported_cost"] = dict(reported_cost)
        return _sanitize(output, token=self._token)


def map_linkedin_row(provider: BrightDataJobsProvider, task: SearchTask, config: BrightDataPlatformConfig,
                     row: Mapping[str, Any], snapshot_id: str, request_shape: Mapping[str, Any],
                     fingerprint: str, part: int) -> AcquisitionRecord:
    if task.platform != "linkedin":
        raise ValueError("LinkedIn mapper received a different source surface")
    return provider._map_row(task, config, row, snapshot_id, request_shape, fingerprint, part, token=provider._token)


def map_indeed_row(provider: BrightDataJobsProvider, task: SearchTask, config: BrightDataPlatformConfig,
                   row: Mapping[str, Any], snapshot_id: str, request_shape: Mapping[str, Any],
                   fingerprint: str, part: int) -> AcquisitionRecord:
    if task.platform != "indeed":
        raise ValueError("Indeed mapper received a different source surface")
    return provider._map_row(task, config, row, snapshot_id, request_shape, fingerprint, part, token=provider._token)


def map_glassdoor_row(provider: BrightDataJobsProvider, task: SearchTask, config: BrightDataPlatformConfig,
                      row: Mapping[str, Any], snapshot_id: str, request_shape: Mapping[str, Any],
                      fingerprint: str, part: int) -> AcquisitionRecord:
    if task.platform != "glassdoor":
        raise ValueError("Glassdoor mapper received a different source surface")
    return provider._map_row(task, config, row, snapshot_id, request_shape, fingerprint, part, token=provider._token)
