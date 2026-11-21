from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import queue
import socket
import ssl
import threading
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
TASK_TIMEOUT_ENV = "JOBBOT_BRIGHTDATA_TASK_TIMEOUT_SECONDS"
DEFAULT_TASK_TIMEOUT_SECONDS = 300.0
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
TOKEN_ENV = "BRIGHTDATA_API_TOKEN"
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
                json_body: Any, token: str) -> BrightDataHTTPResponse: ...


class BrightDataHTTPTransport:
    """HTTP transport with a true overall deadline for each request."""

    def __init__(self, *, timeout_seconds: float = 45, api_root: str = API_ROOT):
        self.timeout_seconds = timeout_seconds
        self.api_root = api_root.rstrip("/")

    def request(self, method: str, path: str, *, params: Mapping[str, Any],
                json_body: Any, token: str, timeout_seconds: float | None = None,
                on_request: Callable[[], None] | None = None) -> BrightDataHTTPResponse:
        query = urllib.parse.urlencode({key: _query_value(value) for key, value in params.items()})
        url = f"{self.api_root}{path}" + (f"?{query}" if query else "")
        data = None if json_body is None else json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        if not token or any(not 33 <= ord(character) <= 126 for character in token):
            raise ValueError("Bright Data token contains invalid header characters")
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        timeout = self.timeout_seconds if timeout_seconds is None else min(self.timeout_seconds, timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise TimeoutError
        deadline = time.monotonic() + timeout
        redirects = 0
        original_origin = _origin(url)
        root = urllib.parse.urlsplit(self.api_root)
        local_http_origin = (
            _origin(self.api_root)
            if root.scheme.casefold() == "http" and (root.hostname or "").casefold() in {"localhost", "127.0.0.1", "::1"}
            else None
        )
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError
            status, response_headers, raw = asyncio.run(
                _async_http_exchange(url, method, headers, data, deadline, on_request),
            )
            if status not in {301, 302, 303, 307, 308}:
                return BrightDataHTTPResponse(status, _decode_payload(raw), response_headers)
            location = _header(response_headers, "location")
            if not location or redirects >= 10:
                return BrightDataHTTPResponse(status, _decode_payload(raw), response_headers)
            next_url = urllib.parse.urljoin(url, location)
            parsed = urllib.parse.urlsplit(next_url)
            is_local_test_redirect = (
                local_http_origin is not None and _origin(next_url) == local_http_origin
            )
            if (parsed.scheme.casefold() != "https" and not is_local_test_redirect) or not parsed.hostname:
                raise OSError("Bright Data returned an unsupported redirect")
            if _origin(next_url) != original_origin:
                headers = {key: value for key, value in headers.items() if key.casefold() != "authorization"}
            if status in {301, 302, 303} and method.upper() != "GET":
                method, data = "GET", None
                headers = {key: value for key, value in headers.items()
                           if key.casefold() not in {"content-type", "content-length"}}
            url = next_url
            redirects += 1


class _BoundedDNSResolver:
    """A single daemon resolver prevents stalled getaddrinfo calls piling up."""

    def __init__(self):
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, int], tuple[float, list[tuple[Any, ...]]]] = {}
        self._pending: dict[tuple[str, int], dict[str, Any]] = {}
        self._queue: queue.Queue[tuple[tuple[str, int], dict[str, Any]]] = queue.Queue()
        self._worker_started = False

    def resolve(self, host: str, port: int, timeout: float) -> list[tuple[Any, ...]]:
        key = (host, port)
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[0] > time.monotonic():
                return list(cached[1])
            pending = self._pending.get(key)
            if pending is None:
                pending = {"event": threading.Event(), "result": None, "error": None}
                self._pending[key] = pending
                self._queue.put((key, pending))
            if not self._worker_started:
                threading.Thread(target=self._resolve_worker, daemon=True,
                                 name="jobbot-brightdata-dns").start()
                self._worker_started = True
        if not pending["event"].wait(max(0.0, timeout)):
            raise TimeoutError
        if pending["error"] is not None:
            raise OSError("Bright Data host name resolution failed") from None
        return list(pending["result"] or [])

    def _resolve_worker(self) -> None:
        while True:
            key, pending = self._queue.get()
            try:
                result = socket.getaddrinfo(key[0], key[1], type=socket.SOCK_STREAM)
                pending["result"] = result
                with self._lock:
                    self._cache[key] = (time.monotonic() + 60.0, result)
            except Exception:
                pending["error"] = True
            finally:
                with self._lock:
                    if self._pending.get(key) is pending:
                        self._pending.pop(key, None)
                pending["event"].set()
                self._queue.task_done()


_DNS_RESOLVER = _BoundedDNSResolver()


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.casefold()
    return scheme, (parsed.hostname or "").casefold(), parsed.port or (443 if scheme == "https" else 80)


def _header(headers: Mapping[str, str], wanted: str) -> str:
    return next((str(value) for key, value in headers.items() if key.casefold() == wanted.casefold()), "")


async def _async_http_exchange(
    url: str, method: str, headers: Mapping[str, str], body: bytes | None, deadline: float,
    on_request: Callable[[], None] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)
    addresses = _DNS_RESOLVER.resolve(host, port, deadline - time.monotonic())
    loop = asyncio.get_running_loop()
    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None
    last_error: Exception | None = None
    for family, socktype, proto, _canonname, sockaddr in addresses:
        sock = socket.socket(family, socktype, proto)
        sock.setblocking(False)
        try:
            async with asyncio.timeout_at(deadline):
                await loop.sock_connect(sock, sockaddr)
                if parsed.scheme.casefold() == "https":
                    reader, writer = await asyncio.open_connection(
                        sock=sock, ssl=ssl.create_default_context(), server_hostname=host,
                        ssl_handshake_timeout=max(0.001, deadline - time.monotonic()),
                    )
                elif parsed.scheme.casefold() == "http":
                    reader, writer = await asyncio.open_connection(sock=sock)
                else:
                    raise OSError("unsupported URL scheme")
            break
        except TimeoutError:
            sock.close()
            raise
        except Exception as exc:
            sock.close()
            last_error = exc
            reader = writer = None
    if reader is None or writer is None:
        raise OSError(type(last_error).__name__ if last_error else "connect failed") from None

    target_path = urllib.parse.quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    target_query = urllib.parse.quote(parsed.query, safe="=&?/:@!$'()*+,;%-._~")
    target = target_path + (f"?{target_query}" if target_query else "")
    if "\r" in target or "\n" in target:
        writer.close()
        raise OSError("invalid HTTP request target")
    display_host = f"[{host}]" if ":" in host else host.encode("idna").decode("ascii")
    default_port = 443 if parsed.scheme.casefold() == "https" else 80
    host_header = display_host if port == default_port else f"{display_host}:{port}"
    outgoing = {"Host": host_header, "Connection": "close", "Accept": "application/json", **headers}
    if body is not None:
        outgoing["Content-Length"] = str(len(body))
    request_bytes = bytearray(f"{method} {target} HTTP/1.1\r\n".encode("ascii"))
    for name, value in outgoing.items():
        if "\r" in name or "\n" in name or "\r" in str(value) or "\n" in str(value):
            writer.close()
            raise ValueError("invalid HTTP header")
        request_bytes.extend(f"{name}: {value}\r\n".encode("latin-1"))
    request_bytes.extend(b"\r\n")
    if body is not None:
        request_bytes.extend(body)

    try:
        async with asyncio.timeout_at(deadline):
            if on_request is not None:
                on_request()
            writer.write(request_bytes)
            await writer.drain()
            status = 0
            response_headers: dict[str, str] = {}
            header_bytes = 0
            while True:
                status_line = await reader.readline()
                header_bytes += len(status_line)
                if not status_line or header_bytes > 65536:
                    raise OSError("invalid HTTP response headers")
                try:
                    version, raw_status, _reason = status_line.decode("latin-1").rstrip("\r\n").split(" ", 2)
                    status = int(raw_status)
                except (ValueError, TypeError):
                    raise OSError("invalid HTTP status line") from None
                if not version.startswith("HTTP/"):
                    raise OSError("invalid HTTP status line")
                response_headers = {}
                while True:
                    line = await reader.readline()
                    header_bytes += len(line)
                    if not line or header_bytes > 65536:
                        raise OSError("invalid HTTP response headers")
                    if line in (b"\r\n", b"\n"):
                        break
                    if b":" not in line:
                        raise OSError("invalid HTTP response header")
                    key, value = line.decode("latin-1").split(":", 1)
                    response_headers[key.strip()] = value.strip()
                if 100 <= status < 200 and status != 101:
                    continue
                break

            if status in {301, 302, 303, 307, 308}:
                return status, response_headers, b""
            if method.upper() == "HEAD" or status in {204, 304}:
                return status, response_headers, b""
            transfer_encoding = _header(response_headers, "transfer-encoding").casefold()
            length_header = _header(response_headers, "content-length")
            chunks = bytearray()
            if "chunked" in transfer_encoding:
                while True:
                    size_line = await reader.readline()
                    if len(size_line) > 8192 or not size_line:
                        raise OSError("invalid chunked response")
                    try:
                        chunk_size = int(size_line.split(b";", 1)[0].strip(), 16)
                    except ValueError:
                        raise OSError("invalid chunked response") from None
                    if chunk_size == 0:
                        while True:
                            trailer = await reader.readline()
                            if trailer in (b"\r\n", b"\n"):
                                break
                            if not trailer:
                                raise OSError("invalid chunked response trailer")
                        break
                    if len(chunks) + chunk_size > MAX_RESPONSE_BYTES:
                        raise OSError("Bright Data response exceeded the supported size bound")
                    chunks.extend(await reader.readexactly(chunk_size))
                    if await reader.readexactly(2) != b"\r\n":
                        raise OSError("invalid chunk framing")
            elif length_header:
                try:
                    length = int(length_header)
                except ValueError:
                    raise OSError("invalid content length") from None
                if length < 0 or length > MAX_RESPONSE_BYTES:
                    raise OSError("Bright Data response exceeded the supported size bound")
                chunks.extend(await reader.readexactly(length))
            else:
                while True:
                    piece = await reader.read(65536)
                    if not piece:
                        break
                    if len(chunks) + len(piece) > MAX_RESPONSE_BYTES:
                        raise OSError("Bright Data response exceeded the supported size bound")
                    chunks.extend(piece)
            return status, response_headers, bytes(chunks)
    finally:
        writer.close()
        remaining = deadline - time.monotonic()
        if remaining > 0:
            try:
                async with asyncio.timeout(remaining):
                    await writer.wait_closed()
            except Exception:
                pass


@dataclass(frozen=True)
class BrightDataRuntimeConfig:
    token: str = field(repr=False)
    platforms: Mapping[str, BrightDataPlatformConfig] = field(default_factory=dict)
    task_timeout_seconds: float = DEFAULT_TASK_TIMEOUT_SECONDS


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
        task_timeout = float(str(env.get(TASK_TIMEOUT_ENV, DEFAULT_TASK_TIMEOUT_SECONDS)).strip())
        task_timeout_valid = math.isfinite(task_timeout) and task_timeout > 0
    except (TypeError, ValueError):
        task_timeout_valid = False
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
        "task_timeout_valid": task_timeout_valid,
        "platform_configs": configured,
        "valid": token_present and task_timeout_valid
        and all(value["valid"] for value in configured.values()),
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
    timeout_raw = str(env.get(TASK_TIMEOUT_ENV, DEFAULT_TASK_TIMEOUT_SECONDS)).strip()
    try:
        task_timeout_seconds = float(timeout_raw)
    except (TypeError, ValueError):
        raise BrightDataConfigurationError(f"{TASK_TIMEOUT_ENV} must be a positive finite number") from None
    if not math.isfinite(task_timeout_seconds) or task_timeout_seconds <= 0:
        raise BrightDataConfigurationError(f"{TASK_TIMEOUT_ENV} must be a positive finite number")
    return BrightDataRuntimeConfig(token=token, platforms=configs,
                                   task_timeout_seconds=task_timeout_seconds)


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
        if not math.isfinite(float(runtime.task_timeout_seconds)) or float(runtime.task_timeout_seconds) <= 0:
            raise BrightDataConfigurationError("Bright Data task timeout must be positive and finite")
        self._token = runtime.token
        self.platforms = dict(runtime.platforms)
        self.candidate = dict(candidate)
        self.max_records = int(max_records)
        self.transport = transport or BrightDataHTTPTransport()
        self.run_id = run_id or f"brightdata:{uuid.uuid4()}"
        self.max_polls = int(max_polls)
        self.max_transient_retries = int(max_transient_retries)
        self.poll_interval_seconds = max(0, float(poll_interval_seconds))
        self.sleep = sleep
        self.batch_size = int(batch_size)
        self.task_timeout_seconds = float(runtime.task_timeout_seconds)
        self.monotonic = monotonic
        self._records_delivered_total = 0
        self._active_started = 0.0
        self._active_deadline = 0.0
        self._active_requests_submitted = 0

    def _remaining_seconds(self) -> float:
        return max(0.0, self._active_deadline - self.monotonic())

    def _note_http_attempt(self) -> None:
        if self._remaining_seconds() <= 0:
            raise TimeoutError
        self._active_requests_submitted += 1

    def _transport_request(self, method: str, path: str, *, params: Mapping[str, Any],
                           json_body: Any) -> BrightDataHTTPResponse:
        remaining = self._remaining_seconds()
        if remaining <= 0:
            raise TimeoutError
        if isinstance(self.transport, BrightDataHTTPTransport):
            response = self.transport.request(
                method, path, params=params, json_body=json_body, token=self._token,
                timeout_seconds=remaining, on_request=self._note_http_attempt,
            )
            if self._remaining_seconds() <= 0:
                raise TimeoutError
            return response
        self._note_http_attempt()
        response = self.transport.request(method, path, params=params, json_body=json_body, token=self._token)
        if self._remaining_seconds() <= 0:
            raise TimeoutError
        return response

    @property
    def records_delivered_total(self) -> int:
        return self._records_delivered_total

    def fetch(self, task: SearchTask) -> ProviderBatch:
        self._active_started = self.monotonic()
        self._active_deadline = self._active_started + self.task_timeout_seconds
        self._active_requests_submitted = 0
        config = self.platforms.get(task.platform)
        if config is None:
            raise ProviderFailure(ProviderFailureClass.CONFIGURATION,
                                  "Bright Data platform configuration is missing", retryable=False)
        remaining = self.max_records - self._records_delivered_total
        task_provenance = {
            "task_key": task.task_key, "query_text": task.query, "window_days": task.age_days,
            "platform": task.platform, "query_family": task.query_family, "query_kind": task.query_kind,
            "query_pass": task.query_pass, "phase": task.phase, "execution_rank": task.execution_rank,
        }
        if remaining <= 0:
            metadata = {"task_provenance": task_provenance, "budget_stop": True,
                        "records_budget": self.max_records, "records_delivered_total": self._records_delivered_total,
                        "task_timeout_seconds": self.task_timeout_seconds,
                        "task_elapsed_seconds": round(max(0.0, self.monotonic() - self._active_started), 3),
                        "requests_submitted": 0}
            return ProviderBatch(
                completion_state=ProviderCompletionState.INCOMPLETE,
                failure_class=ProviderFailureClass.PARTIAL_BATCH,
                provider_task_id="", provider_metadata=metadata,
            )
        input_row = self._input_row(task, config)
        trigger_params: dict[str, Any] = {
            "dataset_id": config.dataset_id, "type": "discover_new", "discover_by": "keyword",
            "include_errors": True, "format": "json", "limit_multiple_results": remaining,
        }
        trigger_shape = {"method": "POST", "path": "/datasets/v3/trigger",
                         "query": dict(trigger_params), "body": [dict(input_row)]}
        if _contains_token(trigger_shape, self._token):
            raise ProviderFailure(ProviderFailureClass.CONFIGURATION,
                                  "Bright Data request configuration contains a secret value", retryable=False)
        fingerprint = hashlib.sha256(_canonical_json(trigger_shape).encode("utf-8")).hexdigest()
        shape: dict[str, Any] = {
            "trigger": trigger_shape,
            "progress_checks": [], "parts_request": None, "result_requests": [],
        }
        requests_submitted = 0
        provider_task_id = ""
        actual_records = 0
        reported_cost: dict[str, Any] = {}
        base_metadata = {
            "task_provenance": task_provenance, "request_shape": shape,
            "request_fingerprint": fingerprint, "records_budget": self.max_records,
            "records_delivered_before_task": self._records_delivered_total,
            "task_timeout_seconds": self.task_timeout_seconds,
        }
        try:
            triggered = self._transport_request(
                "POST", trigger_shape["path"], params=trigger_params, json_body=[dict(input_row)],
            )
        except (TimeoutError, socket.timeout):
            raise self._failure(ProviderFailureClass.TIMEOUT, "Bright Data trigger timed out",
                                base_metadata, self._active_requests_submitted, 0, reported_cost) from None
        except Exception as exc:
            raise self._failure(ProviderFailureClass.TRANSPORT,
                                f"Bright Data trigger transport failed ({type(exc).__name__})",
                                base_metadata, self._active_requests_submitted, 0, reported_cost) from None
        requests_submitted = self._active_requests_submitted
        reported_cost.update(_reported_cost(triggered.payload, token=self._token))
        if triggered.status in (401, 403):
            raise self._failure(ProviderFailureClass.AUTHORIZATION,
                                f"Bright Data trigger returned HTTP {triggered.status}",
                                base_metadata, requests_submitted, 0, reported_cost, retryable=False)
        if triggered.status in (429,) or 500 <= triggered.status <= 599:
            raise self._failure(ProviderFailureClass.TRANSPORT,
                                f"Bright Data trigger returned transient HTTP {triggered.status}",
                                base_metadata, requests_submitted, 0, reported_cost)
        if triggered.status in (400, 422):
            raise self._failure(ProviderFailureClass.SCHEMA,
                                f"Bright Data rejected configured input with HTTP {triggered.status}",
                                base_metadata, requests_submitted, 0, reported_cost, retryable=False)
        if triggered.status == 404:
            raise self._failure(ProviderFailureClass.CONFIGURATION,
                                "Bright Data dataset configuration was not found (HTTP 404)",
                                base_metadata, requests_submitted, 0, reported_cost, retryable=False)
        if triggered.status not in (200, 202) or not isinstance(triggered.payload, Mapping):
            raise self._failure(ProviderFailureClass.INVALID_RESPONSE,
                                "Bright Data trigger returned an invalid acknowledgement",
                                base_metadata, requests_submitted, 0, reported_cost, retryable=False)
        provider_task_id = _scalar(triggered.payload.get("snapshot_id"))
        if not provider_task_id or self._token in provider_task_id:
            raise self._failure(ProviderFailureClass.INVALID_RESPONSE,
                                "Bright Data trigger acknowledgement omitted a safe snapshot_id",
                                base_metadata, requests_submitted, 0, reported_cost, retryable=False)
        base_metadata["snapshot_id"] = provider_task_id

        for poll_index in range(self.max_polls):
            if poll_index:
                remaining_time = self._remaining_seconds()
                if remaining_time <= 0:
                    raise self._failure(
                        ProviderFailureClass.TIMEOUT, "Bright Data task exceeded its total time budget",
                        base_metadata, self._active_requests_submitted, actual_records, reported_cost,
                    )
                self.sleep(min(self.poll_interval_seconds, remaining_time))
                if self._remaining_seconds() <= 0:
                    raise self._failure(
                        ProviderFailureClass.TIMEOUT, "Bright Data task exceeded its total time budget",
                        base_metadata, self._active_requests_submitted, actual_records, reported_cost,
                    )
            progress_path = f"/datasets/v3/progress/{urllib.parse.quote(provider_task_id, safe='')}"
            progress_request = {"method": "GET", "path": progress_path, "query": {}}
            shape["progress_checks"].append(progress_request)
            progress = self._get(progress_path, {}, base_metadata, requests_submitted,
                                 actual_records, reported_cost, "progress")
            reported_cost.update(_reported_cost(progress.payload, token=self._token))
            if progress.status != 200 or not isinstance(progress.payload, Mapping):
                raise self._failure(ProviderFailureClass.INVALID_RESPONSE,
                                    "Bright Data progress response was malformed",
                                    base_metadata, requests_submitted, actual_records, reported_cost, retryable=False)
            progress_status = str(progress.payload.get("status") or "").casefold()
            if progress_status in {"starting", "running"}:
                continue
            if progress_status in {"failed", "canceled"}:
                classification, message = _status_failure(
                    progress_status, _scalar(progress.payload.get("error_message")),
                )
                reported_cost.update(_reported_cost(progress.payload, token=self._token))
                raise self._failure(classification, message, base_metadata, requests_submitted,
                                    actual_records, reported_cost, retryable=False)
            if progress_status != "ready":
                raise self._failure(ProviderFailureClass.INVALID_RESPONSE,
                                    "Bright Data progress returned an unknown terminal state",
                                    base_metadata, requests_submitted, actual_records, reported_cost, retryable=False)

            parts_path = f"/datasets/v3/snapshot/{urllib.parse.quote(provider_task_id, safe='')}/parts"
            parts_params = {"batch_size": self.batch_size}
            shape["parts_request"] = {"method": "GET", "path": parts_path, "query": dict(parts_params)}
            parts_response = self._get(parts_path, parts_params, base_metadata, requests_submitted,
                                       actual_records, reported_cost, "parts")
            reported_cost.update(_reported_cost(parts_response.payload, token=self._token))
            if parts_response.status != 200 or not isinstance(parts_response.payload, Mapping):
                raise self._failure(ProviderFailureClass.AMBIGUOUS_CURSOR,
                                    "Bright Data did not provide snapshot part-completion evidence",
                                    base_metadata, requests_submitted, actual_records, reported_cost)
            parts_total = parts_response.payload.get("parts")
            if isinstance(parts_total, bool) or not isinstance(parts_total, int) or parts_total < 1:
                raise self._failure(ProviderFailureClass.AMBIGUOUS_CURSOR,
                                    "Bright Data snapshot part count was missing or invalid",
                                    base_metadata, requests_submitted, actual_records, reported_cost)
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
                })
                return ProviderBatch(
                    records=tuple(mapped_records[:remaining_for_task]),
                    completion_state=(ProviderCompletionState.RETRYABLE if failure.retryable
                                      else ProviderCompletionState.INCOMPLETE),
                    failure_class=failure.classification, provider_task_id=provider_task_id,
                    provider_metadata=self._provider_metadata(
                        {**dict(failure.provider_metadata), **base_metadata}, shape, fingerprint, reported_cost,
                    ),
                    requests_submitted=self._active_requests_submitted, records_delivered=actual_records,
                    reported_cost=reported_cost,
                )

            for part in range(1, parts_total + 1):
                if self._records_delivered_total >= self.max_records:
                    break
                result_path = f"/datasets/v3/snapshot/{urllib.parse.quote(provider_task_id, safe='')}"
                result_params = {"format": "json", "batch_size": self.batch_size, "part": part}
                shape["result_requests"].append({"method": "GET", "path": result_path, "query": dict(result_params)})
                try:
                    result = self._get(result_path, result_params, base_metadata, requests_submitted,
                                       actual_records, reported_cost, "result")
                except ProviderFailure as exc:
                    return partial_result_failure(exc)
                reported_cost.update(_reported_cost(result.payload, token=self._token))
                if result.status in (202, 404, 409):
                    return partial_result_failure(self._failure(
                        ProviderFailureClass.PARTIAL_BATCH,
                        "Bright Data snapshot result part is not available",
                        base_metadata, requests_submitted, actual_records, reported_cost,
                    ))
                if result.status == 200 and result.payload in (None, ""):
                    return partial_result_failure(self._failure(
                        ProviderFailureClass.PARTIAL_BATCH,
                        "Bright Data snapshot result part was empty or unavailable",
                        base_metadata, requests_submitted, actual_records, reported_cost,
                    ))
                if result.status != 200:
                    return partial_result_failure(self._failure(
                        ProviderFailureClass.INVALID_RESPONSE,
                        "Bright Data snapshot result returned an invalid HTTP status",
                        base_metadata, requests_submitted, actual_records, reported_cost,
                        retryable=False,
                    ))
                if isinstance(result.payload, Mapping) and str(result.payload.get("status") or "").casefold() in {
                    "starting", "running", "building",
                }:
                    return partial_result_failure(self._failure(
                        ProviderFailureClass.PARTIAL_BATCH,
                        "Bright Data snapshot result part is still pending",
                        base_metadata, requests_submitted, actual_records, reported_cost,
                    ))
                try:
                    rows, envelope = _row_page(result.payload, config.result_rows_key)
                except ValueError as exc:
                    return partial_result_failure(self._failure(
                        ProviderFailureClass.INVALID_RESPONSE, str(exc), base_metadata,
                        requests_submitted, actual_records, reported_cost, retryable=False,
                    ))
                if self._remaining_seconds() <= 0:
                    return partial_result_failure(self._failure(
                        ProviderFailureClass.TIMEOUT,
                        "Bright Data task exceeded its total time budget",
                        base_metadata, self._active_requests_submitted, actual_records, reported_cost,
                    ))
                more, marker = _continuation(envelope)
                if more:
                    continuation.update(marker)
                part_numbers.append(part)
                actual_records += len(rows)
                self._records_delivered_total += len(rows)
                for row in rows:
                    if self._remaining_seconds() <= 0:
                        return partial_result_failure(self._failure(
                            ProviderFailureClass.TIMEOUT,
                            "Bright Data task exceeded its total time budget",
                            base_metadata, self._active_requests_submitted, actual_records, reported_cost,
                        ))
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
                    if self._remaining_seconds() <= 0:
                        return partial_result_failure(self._failure(
                            ProviderFailureClass.TIMEOUT,
                            "Bright Data task exceeded its total time budget",
                            base_metadata, self._active_requests_submitted, actual_records, reported_cost,
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
            if self._remaining_seconds() <= 0:
                return partial_result_failure(self._failure(
                    ProviderFailureClass.TIMEOUT,
                    "Bright Data task exceeded its total time budget",
                    base_metadata, self._active_requests_submitted, actual_records, reported_cost,
                ))
            capped = actual_count >= remaining
            parts_missing = len(part_numbers) < parts_total
            incomplete = bool(continuation or invalid_rows or provider_errors or capped or parts_missing)
            evidence = {
                "platform": task.platform, "task_key": task.task_key,
                "provider_job_id": provider_task_id, "terminal_state": progress_status,
                "batch": {"parts_total": parts_total, "parts_retrieved": len(part_numbers),
                          "part_numbers": part_numbers, "records_delivered": actual_count,
                          "continuation": _sanitize(continuation, token=self._token)},
                "request_fingerprint": fingerprint,
            }
            base_metadata["completion_observation"] = evidence
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
                requests_submitted=self._active_requests_submitted,
                records_delivered=actual_count,
                reported_cost=reported_cost,
            )

        raise self._failure(ProviderFailureClass.TIMEOUT,
                            "Bright Data collection did not reach terminal ready state within the poll bound",
                            base_metadata, requests_submitted, actual_records, reported_cost)

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
             requests_submitted: int, records_delivered: int, reported_cost: Mapping[str, Any],
             operation: str) -> BrightDataHTTPResponse:
        last_error = ""
        for attempt in range(self.max_transient_retries + 1):
            try:
                response = self._transport_request("GET", path, params=params, json_body=None)
            except (TimeoutError, socket.timeout):
                last_error = "timeout"
                if attempt < self.max_transient_retries:
                    continue
                raise self._failure(ProviderFailureClass.TIMEOUT,
                                    f"Bright Data {operation} request timed out", metadata,
                                    requests_submitted, records_delivered, reported_cost) from None
            except Exception as exc:
                last_error = type(exc).__name__
                if attempt < self.max_transient_retries:
                    continue
                raise self._failure(ProviderFailureClass.TRANSPORT,
                                    f"Bright Data {operation} transport failed ({last_error})", metadata,
                                    requests_submitted, records_delivered, reported_cost) from None
            if response.status in (429,) or 500 <= response.status <= 599:
                if attempt < self.max_transient_retries:
                    continue
                raise self._failure(ProviderFailureClass.TRANSPORT,
                                    f"Bright Data {operation} returned transient HTTP {response.status}", metadata,
                                    requests_submitted, records_delivered, reported_cost)
            if response.status in (401, 403):
                raise self._failure(ProviderFailureClass.AUTHORIZATION,
                                    f"Bright Data {operation} returned HTTP {response.status}", metadata,
                                    requests_submitted, records_delivered, reported_cost, retryable=False)
            if response.status == 404 and operation in {"progress", "parts"}:
                raise self._failure(ProviderFailureClass.CONFIGURATION,
                                    f"Bright Data {operation} endpoint returned HTTP 404", metadata,
                                    requests_submitted, records_delivered, reported_cost, retryable=False)
            return response
        raise self._failure(ProviderFailureClass.TRANSPORT,
                            f"Bright Data {operation} request failed ({last_error})", metadata,
                            requests_submitted, records_delivered, reported_cost)

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
            requests_submitted=self._active_requests_submitted, records_delivered=records_delivered,
            reported_cost=reported_cost,
        )

    def _provider_metadata(self, metadata: Mapping[str, Any], request_shape: Mapping[str, Any],
                           fingerprint: str, reported_cost: Mapping[str, Any]) -> dict[str, Any]:
        output = dict(metadata)
        output["task_timeout_seconds"] = self.task_timeout_seconds
        output["task_elapsed_seconds"] = round(max(0.0, self.monotonic() - self._active_started), 3)
        output["requests_submitted"] = self._active_requests_submitted
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
