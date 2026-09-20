"""Safe, disposable crawl-observation cache for CHG-146.

This store is deliberately not the ledger.  It contains only normalized public
job evidence and can be deleted or become unavailable without changing funnel
state, qualification, applications, or exports.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import ConfigBundle, resolve_path
from .db import configure_connection


SCHEMA_VERSION = 1
SCHEMA_NAME = "chg146-safe-crawl-observations"
DEFAULT_FRESH_HOURS = 72
FORBIDDEN_KEYS = {
    "cookie", "cookies", "credential", "credentials", "password", "token",
    "access_token", "refresh_token", "bridge_token", "session", "session_id",
    "captcha", "captcha_material", "browser_profile", "profile_data",
}


def observation_path(bundle: ConfigBundle) -> Path:
    configured = bundle.runtime.get("runtime", {}).get("crawl_observations_path")
    return resolve_path(configured or "data/crawl_observations.sqlite3", root=bundle.root)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _assert_safe(value: Any, path: str = "") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).casefold()
            if lowered in FORBIDDEN_KEYS or any(marker in lowered for marker in ("cookie", "password", "secret", "token")):
                raise ValueError(f"unsafe observation field: {path + str(key)}")
            _assert_safe(item, f"{path}{key}.")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_safe(item, f"{path}{index}.")


def card_hash(card: dict[str, Any] | None, *, source_job_id: str, source_url: str, title: str = "", company: str = "", location: str = "") -> str:
    value = {
        "source_job_id": source_job_id,
        "source_url": source_url,
        "title": title,
        "company": company,
        "location": location,
        "card": card if isinstance(card, dict) else {},
    }
    _assert_safe(value)
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def detail_hash(job: dict[str, Any], evidence: dict[str, Any]) -> str:
    safe_job = {key: job.get(key, "") for key in (
        "source_job_id", "canonical_url", "title", "company", "location",
        "remote_status", "employment_type", "salary_text", "posted_at",
        "description", "valid_through",
    )}
    provenance = evidence.get("detail_acquisition") if isinstance(evidence, dict) else {}
    value = {"job": safe_job, "detail_acquisition": provenance if isinstance(provenance, dict) else {}}
    _assert_safe(value)
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def connect(bundle: ConfigBundle) -> sqlite3.Connection:
    path = observation_path(bundle)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        configure_connection(conn, busy_timeout_ms=5000, synchronous="NORMAL")
        ensure_schema(conn)
        return conn
    except Exception:
        conn.close()
        raise


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS observation_schema(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
    conn.execute("""CREATE TABLE IF NOT EXISTS crawl_observations(
      observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
      platform TEXT NOT NULL,
      source_job_id TEXT NOT NULL,
      source_url TEXT NOT NULL,
      card_hash TEXT NOT NULL,
      detail_hash TEXT NOT NULL DEFAULT '',
      observed_at TEXT NOT NULL,
      normalized_card_json TEXT NOT NULL DEFAULT '{}',
      detail_job_json TEXT NOT NULL DEFAULT '{}',
      detail_provenance_json TEXT NOT NULL DEFAULT '{}',
      content_completeness TEXT NOT NULL DEFAULT 'MISSING',
      source_build TEXT NOT NULL DEFAULT '',
      schema_version INTEGER NOT NULL DEFAULT 1,
      last_used_at TEXT,
      UNIQUE(platform,source_job_id,source_url)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_observations_lookup ON crawl_observations(platform,source_job_id,source_url,content_completeness,observed_at)")
    row = conn.execute("SELECT version FROM observation_schema ORDER BY version DESC LIMIT 1").fetchone()
    if row is None:
        conn.execute("INSERT INTO observation_schema(version,name,applied_at) VALUES(?,?,?)", (SCHEMA_VERSION, SCHEMA_NAME, _now()))
    conn.commit()


def publish(
    bundle: ConfigBundle, *, platform: str, source_job_id: str, source_url: str,
    card: dict[str, Any] | None, title: str, company: str, location: str,
    job: dict[str, Any], evidence: dict[str, Any], source_build: str,
) -> bool:
    """Publish only after the caller has durably validated the canonical job."""
    try:
        _assert_safe(card or {})
        _assert_safe(evidence)
        normalized_card = {
            "title": title, "company": company, "location": location,
            "posted_text": (card or {}).get("posted_text", ""),
            "posted_age_days": (card or {}).get("posted_age_days"),
        }
        normalized_job = {key: job.get(key, "") for key in (
            "source_job_id", "canonical_url", "title", "company", "location",
            "remote_status", "employment_type", "salary_text", "posted_at",
            "description", "valid_through",
        )}
        provenance = evidence.get("detail_acquisition", {}) if isinstance(evidence, dict) else {}
        completeness = "COMPLETE" if len(str(normalized_job.get("description", "")).strip()) >= 250 else "PARTIAL"
        now = _now()
        conn = None
        try:
            conn = connect(bundle)
            conn.execute("""INSERT INTO crawl_observations(
              platform,source_job_id,source_url,card_hash,detail_hash,observed_at,
              normalized_card_json,detail_job_json,detail_provenance_json,content_completeness,source_build,schema_version
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(platform,source_job_id,source_url) DO UPDATE SET
              card_hash=excluded.card_hash,detail_hash=excluded.detail_hash,observed_at=excluded.observed_at,
              normalized_card_json=excluded.normalized_card_json,detail_job_json=excluded.detail_job_json,
              detail_provenance_json=excluded.detail_provenance_json,content_completeness=excluded.content_completeness,
              source_build=excluded.source_build,schema_version=excluded.schema_version""",
                (platform, source_job_id, source_url, card_hash(card, source_job_id=source_job_id, source_url=source_url, title=title, company=company, location=location),
                 detail_hash(normalized_job, evidence), now, _json(normalized_card), _json(normalized_job), _json(provenance), completeness, source_build, SCHEMA_VERSION),
            )
            conn.commit()
        except (OSError, sqlite3.Error, TypeError, ValueError):
            if conn is not None:
                conn.rollback()
            raise
        finally:
            if conn is not None:
                conn.close()
        return True
    except (OSError, sqlite3.Error, TypeError, ValueError):
        # The production ledger must never fail because an optional cache is
        # corrupt, locked, unavailable, or presented unsafe content.
        return False


def lookup(
    bundle: ConfigBundle, *, platform: str, source_job_id: str, source_url: str,
    current_card_hash: str, max_age_hours: int = DEFAULT_FRESH_HOURS,
    source_build: str = "", allow_reuse: bool = True,
) -> dict[str, Any] | None:
    if not allow_reuse:
        return None
    try:
        conn = None
        try:
            conn = connect(bundle)
            row = conn.execute("""SELECT * FROM crawl_observations
              WHERE platform=? AND source_job_id=? AND source_url=?
                AND content_completeness='COMPLETE'
                AND card_hash=? AND observed_at>=?
                AND (?='' OR source_build=?)
              ORDER BY observed_at DESC LIMIT 1""",
                (platform, source_job_id, source_url, current_card_hash,
                 (datetime.now(timezone.utc) - timedelta(hours=max(1, max_age_hours))).isoformat(timespec="seconds"), source_build, source_build)).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE crawl_observations SET last_used_at=? WHERE observation_id=?", (_now(), row["observation_id"]))
            conn.commit()
            return {
                "observation_id": int(row["observation_id"]),
                "platform": row["platform"], "source_job_id": row["source_job_id"], "source_url": row["source_url"],
                "card_hash": row["card_hash"], "observed_at": row["observed_at"],
                "job": json.loads(row["detail_job_json"] or "{}"),
                "detail_acquisition": {**json.loads(row["detail_provenance_json"] or "{}"), "mode": "cache", "cache_observation_id": int(row["observation_id"])},
                "provenance": "crawl_observation_cache",
            }
        finally:
            if conn is not None:
                conn.close()
    except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
        return None


def stats(bundle: ConfigBundle) -> dict[str, Any]:
    try:
        conn = None
        try:
            conn = connect(bundle)
            row = conn.execute("SELECT COUNT(*) total,COALESCE(SUM(content_completeness='COMPLETE'),0) complete FROM crawl_observations").fetchone()
            return {"path": str(observation_path(bundle)), "available": True, "total": int(row["total"]), "complete": int(row["complete"]), "schema_version": SCHEMA_VERSION}
        finally:
            if conn is not None:
                conn.close()
    except (OSError, sqlite3.Error):
        return {"path": str(observation_path(bundle)), "available": False, "total": 0, "complete": 0, "schema_version": SCHEMA_VERSION}
