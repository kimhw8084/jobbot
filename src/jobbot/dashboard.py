from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .application import ApplicationError, STATUSES, add_note, mark
from .config import ConfigBundle
from .db import Database
from .exports import export_selected


TABLE_COLUMNS = (
    "job_id", "recommendation", "title", "company", "career_lane", "sources", "posted_at",
    "first_seen", "last_seen", "salary_text", "employment_class", "remote_gate", "eligible_states_json", "location_raw",
    "relevance_score", "qualification_score", "landing_score", "career_score", "door_score",
    "resume_variant", "application_status", "change_status", "description_state",
)


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else {key: row[key] for key in row.keys()}


def database_identity(conn: sqlite3.Connection, bundle: ConfigBundle) -> str:
    versions = ",".join(str(row[0]) for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version"))
    value = f"{bundle.database_path.resolve()}|{versions}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def identity(conn: sqlite3.Connection, bundle: ConfigBundle) -> dict[str, Any]:
    return {
        "jobbot_version": "3.2.1",
        "workspace_root": str(bundle.root.resolve()),
        "resolved_database_path": str(bundle.database_path.resolve()),
        "database_identity": database_identity(conn, bundle),
        "pid": os.getpid(),
    }


def summary(conn: sqlite3.Connection) -> dict[str, int]:
    queries = {
        "total": "SELECT COUNT(*) FROM jobs", "active": "SELECT COUNT(*) FROM jobs WHERE is_active=1",
        "updated": "SELECT COUNT(*) FROM jobs WHERE change_status='UPDATED'",
        "descriptions_complete": "SELECT COUNT(*) FROM jobs WHERE length(trim(COALESCE(description,''))) >= 250",
        "remote_confirmed": "SELECT COUNT(*) FROM jobs WHERE remote_gate='pass'",
        "remote_review": "SELECT COUNT(*) FROM jobs WHERE remote_gate='review'",
        "remote_rejected": "SELECT COUNT(*) FROM jobs WHERE remote_gate='reject'",
        "qualified": "SELECT COUNT(*) FROM jobs WHERE is_active=1 AND recommendation IN ('APPLY_NOW','APPLY_VOLUME','HIGH_VALUE_STRETCH')",
        "apply_now": "SELECT COUNT(*) FROM jobs WHERE is_active=1 AND recommendation='APPLY_NOW'",
        "apply_volume": "SELECT COUNT(*) FROM jobs WHERE is_active=1 AND recommendation='APPLY_VOLUME'",
        "stretch": "SELECT COUNT(*) FROM jobs WHERE is_active=1 AND recommendation='HIGH_VALUE_STRETCH'",
        "reservoir": """SELECT COUNT(*) FROM jobs WHERE is_active=1 AND remote_gate='pass'
          AND recommendation IN ('APPLY_NOW','APPLY_VOLUME','HIGH_VALUE_STRETCH')
          AND upper(application_status) NOT IN ('APPLIED','SCREEN','INTERVIEW','FINAL','OFFER','REJECTED','WITHDRAWN','SKIP','CLOSED')""",
        "applied": "SELECT COUNT(*) FROM jobs WHERE upper(application_status) IN ('APPLIED','SCREEN','INTERVIEW','FINAL','OFFER','REJECTED')",
        "screens": "SELECT COUNT(*) FROM jobs WHERE upper(application_status)='SCREEN'",
        "interviews": "SELECT COUNT(*) FROM jobs WHERE upper(application_status)='INTERVIEW'",
        "finals": "SELECT COUNT(*) FROM jobs WHERE upper(application_status)='FINAL'",
        "offers": "SELECT COUNT(*) FROM jobs WHERE upper(application_status)='OFFER'",
        "applications": "SELECT COUNT(*) FROM application_events",
        "discoveries": "SELECT COUNT(*) FROM search_task_results",
        "detail_pending": "SELECT COUNT(*) FROM search_task_results WHERE detail_status IN ('PENDING','RUNNING','RETRYABLE','EXTERNAL_BLOCKED')",
        "details_complete": "SELECT COUNT(*) FROM search_task_results WHERE detail_status='COMPLETE'",
    }
    return {key: int(conn.execute(sql).fetchone()[0] or 0) for key, sql in queries.items()}


def strategy_info(bundle: ConfigBundle) -> dict[str, Any]:
    strategy = bundle.strategy.get("strategy", {})
    lanes = []
    for lane in bundle.strategy.get("lanes", []):
        lanes.append({
            "id": lane.get("id", ""), "label": lane.get("label", ""),
            "allocation_percent": lane.get("allocation_percent", 0),
            "execution_rank": lane.get("execution_rank", 1000), "resume_variant": lane.get("resume_variant", ""),
        })
    scoring = strategy.get("scoring", {})
    return {
        "positioning": "Bilingual Healthcare Operations & Data Quality Specialist",
        "thesis": "Bilingual regulated healthcare operations → health information, documentation, and data quality → QA, compliance, and process improvement → analytics, implementation, and project ownership → senior analyst, lead, program, quality, implementation, and operations paths.",
        "governing_sentence": "Use enrollment to get the next job. Do not allow enrollment to define the next decade.",
        "portfolio": lanes,
        "score_weights": {"landing_qualification": scoring.get("landing_qualification_weight", 0.70), "door_landing": scoring.get("door_landing_weight", 0.70), "door_career": scoring.get("door_career_weight", 0.30)},
        "recommendations": {
            "APPLY_NOW": "Strong fit, confirmed remote, active, and worth applying to now.",
            "APPLY_VOLUME": "Legitimate qualified role with lower strategic upside but worthwhile application value.",
            "HIGH_VALUE_STRETCH": "Strong career direction with a real but potentially bridgeable qualification gap.",
            "REVIEW": "Needs a human check before deciding.",
            "REVIEW_REMOTE": "Remote or state eligibility is not resolved confidently.",
            "CONTRACT_REVIEW": "Contract or fixed-term work is separated from the permanent reservoir.",
            "OUT_OF_SCOPE": "Stored for audit, but occupation or career relevance is below the actionable bar.",
        },
    }


def coverage(conn: sqlite3.Connection) -> dict[str, Any]:
    primary: dict[str, dict[str, int]] = {}
    for platform in ("linkedin", "indeed", "glassdoor"):
        row = conn.execute("""SELECT COUNT(*) total,
          COALESCE(SUM(status='exhausted'),0) exhausted,
          COALESCE(SUM(status='incomplete'),0) incomplete,
          COALESCE(SUM(status='challenged'),0) challenged,
          COALESCE(SUM(status='auth_required'),0) auth_required,
          COALESCE(SUM(status='deferred_by_platform'),0) deferred,
          COALESCE(SUM(status='failed'),0) failed,
          COALESCE(SUM(results_seen),0) results,
          COALESCE(SUM(detail_count_read),0) details,
          COALESCE(SUM(unique_jobs_recorded),0) unique_jobs
          FROM browser_search_tasks WHERE platform=?""", (platform,)).fetchone()
        primary[platform] = {key: int(row[key] or 0) for key in row.keys()}
    supplemental = {
        str(row["source_site"]): int(row["jobs"])
        for row in conn.execute("""SELECT source_site,COUNT(DISTINCT job_id) jobs
          FROM source_occurrences WHERE source_site NOT IN ('linkedin','indeed','glassdoor')
          GROUP BY source_site ORDER BY jobs DESC""")
    }
    return {"primary": primary, "supplemental": supplemental}


def active_run(conn: sqlite3.Connection) -> dict[str, Any]:
    watch = conn.execute("SELECT * FROM watch_state WHERE watch_id=1").fetchone()
    run = conn.execute("SELECT * FROM browser_runs ORDER BY browser_run_id DESC LIMIT 1").fetchone()
    if run is None:
        return {"run": None, "platforms": [], "watch": _dict(watch)}
    run_id = int(run["browser_run_id"])
    platforms = conn.execute(
        """SELECT p.*,
          COALESCE((SELECT COUNT(*) FROM browser_search_tasks t WHERE t.browser_run_id=p.browser_run_id AND t.platform=p.platform AND t.status='running'),0) running,
          COALESCE((SELECT COUNT(*) FROM browser_search_tasks t WHERE t.browser_run_id=p.browser_run_id AND t.platform=p.platform AND t.status='deferred_by_platform'),0) deferred,
          COALESCE((SELECT COUNT(*) FROM search_task_results r JOIN browser_search_tasks t ON t.task_id=r.task_id WHERE t.browser_run_id=p.browser_run_id AND t.platform=p.platform),0) discoveries,
          COALESCE((SELECT COUNT(*) FROM search_task_results r JOIN browser_search_tasks t ON t.task_id=r.task_id WHERE t.browser_run_id=p.browser_run_id AND t.platform=p.platform AND r.detail_status='COMPLETE'),0) details_complete,
          COALESCE((SELECT SUM(t.cards_extracted) FROM browser_search_tasks t WHERE t.browser_run_id=p.browser_run_id AND t.platform=p.platform),0) cards_extracted,
          COALESCE((SELECT SUM(t.cards_persistence_succeeded) FROM browser_search_tasks t WHERE t.browser_run_id=p.browser_run_id AND t.platform=p.platform),0) cards_persisted,
          COALESCE((SELECT SUM(t.cards_persistence_failed) FROM browser_search_tasks t WHERE t.browser_run_id=p.browser_run_id AND t.platform=p.platform),0) cards_failed,
          COALESCE((SELECT SUM(t.duplicate_cards) FROM browser_search_tasks t WHERE t.browser_run_id=p.browser_run_id AND t.platform=p.platform),0) duplicate_cards
          FROM browser_platform_runs p WHERE p.browser_run_id=?
          ORDER BY CASE p.platform WHEN 'linkedin' THEN 0 WHEN 'indeed' THEN 1 ELSE 2 END""", (run_id,),
    ).fetchall()
    current = conn.execute(
        """SELECT task_id,platform,phase,career_lane,window_days,execution_rank,priority,query_text,status,page_number,results_seen,detail_count_read,
          cards_extracted,cards_persistence_attempted,cards_persistence_succeeded,cards_persistence_failed,
          duplicate_cards,pending_details,details_failed,current_search_url,last_progress_at,last_error
          FROM browser_search_tasks WHERE task_id=?""",
        (run["current_task_id"],),
    ).fetchone() if run["current_task_id"] else None
    return {"run": _dict(run), "current_task": _dict(current), "platforms": [_dict(row) for row in platforms], "watch": _dict(watch)}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _control_run_id(conn: sqlite3.Connection, requested: Any) -> int:
    if requested not in (None, "", 0, "0"):
        run_id = int(requested)
        if conn.execute("SELECT 1 FROM browser_runs WHERE browser_run_id=?", (run_id,)).fetchone() is None:
            raise ValueError(f"browser run not found: {run_id}")
        return run_id
    row = conn.execute(
        """SELECT browser_run_id FROM browser_runs
           WHERE status IN ('queued','running','partial','stopped')
              OR EXISTS (SELECT 1 FROM browser_search_tasks t
                         WHERE t.browser_run_id=browser_runs.browser_run_id
                           AND t.status IN ('queued','running','stopped','incomplete'))
           ORDER BY browser_run_id DESC LIMIT 1"""
    ).fetchone()
    if row is None:
        raise ValueError("no resumable browser run found")
    return int(row[0])


def control_run(conn: sqlite3.Connection, action: str, requested_run_id: Any = None) -> dict[str, Any]:
    """Apply a dashboard run control to durable state and commit immediately."""
    run_id = _control_run_id(conn, requested_run_id)
    run = conn.execute("SELECT status FROM browser_runs WHERE browser_run_id=?", (run_id,)).fetchone()
    if run is None:
        raise ValueError(f"browser run not found: {run_id}")
    current_status = str(run[0])
    now = _utc_now()
    if action == "stop":
        if current_status in {"completed", "partial", "failed"}:
            raise ValueError(f"run {run_id} is already terminal ({current_status})")
        conn.execute(
            "UPDATE browser_runs SET stop_after_current=1,last_error=?,last_progress_at=? WHERE browser_run_id=?",
            ("stop after current job requested from dashboard", now, run_id),
        )
        message = "Stop requested; the current atomic job will finish before acquisition stops."
    elif action == "emergency":
        if current_status == "completed":
            raise ValueError(f"run {run_id} is already terminal (completed)")
        conn.execute(
            """UPDATE search_task_results
               SET detail_status='RETRYABLE',detail_error='requeued after dashboard emergency stop',
                   detail_lease_owner='',detail_lease_until=NULL
               WHERE browser_run_id=? AND detail_status='RUNNING'""",
            (run_id,),
        )
        conn.execute(
            """UPDATE browser_search_tasks
               SET status='incomplete',completed_at=?,lease_owner='',lease_until=NULL,
                   safety_stop_reason='manual_emergency_stop',last_error='manual emergency stop',last_progress_at=?
               WHERE browser_run_id=? AND status='running'""",
            (now, now, run_id),
        )
        conn.execute(
            """UPDATE browser_runs
               SET status='stopped',stop_requested=1,stop_after_current=0,completed_at=?,current_task_id=NULL,
                   tasks_incomplete=(SELECT COUNT(*) FROM browser_search_tasks WHERE browser_run_id=? AND status='incomplete'),
                   last_progress_at=?,last_error='manual emergency stop'
               WHERE browser_run_id=?""",
            (now, run_id, now, run_id),
        )
        message = "Emergency stop persisted; unfinished task and detail work remain resumable."
    elif action == "resume":
        conn.execute(
            """UPDATE browser_search_tasks
               SET status='queued',completed_at=NULL,lease_owner='',lease_until=NULL,last_error='',safety_stop_reason=''
               WHERE browser_run_id=? AND (status IN ('running','stopped') OR
                 (status='incomplete' AND safety_stop_reason NOT LIKE 'Acceptance limit reached%'))""",
            (run_id,),
        )
        conn.execute(
            """UPDATE search_task_results
               SET detail_status='RETRYABLE',detail_lease_owner='',detail_lease_until=NULL,
                   detail_error=CASE WHEN detail_error='' THEN 'requeued after dashboard resume' ELSE detail_error END
               WHERE browser_run_id=? AND detail_status='RUNNING'""",
            (run_id,),
        )
        conn.execute(
            """UPDATE browser_search_tasks
               SET status='queued',completed_at=NULL,lease_owner='',lease_until=NULL,
                   challenge_reason='',last_error=''
               WHERE browser_run_id=? AND status IN ('auth_required','deferred_by_platform')
                 AND platform IN (SELECT platform FROM browser_platform_runs
                                  WHERE browser_run_id=? AND auth_status='not_authenticated')""",
            (run_id, run_id),
        )
        conn.execute(
            """UPDATE browser_search_tasks
               SET status='queued',completed_at=NULL,lease_owner='',lease_until=NULL,
                   challenge_reason='',last_error=''
               WHERE browser_run_id=? AND status IN ('challenged','deferred_by_platform')
                 AND platform IN (SELECT platform FROM browser_platform_runs
                                  WHERE browser_run_id=? AND COALESCE(cooldown_until,'')<=?)""",
            (run_id, run_id, now),
        )
        conn.execute(
            """UPDATE search_task_results SET detail_status='RETRYABLE',detail_lease_owner='',detail_lease_until=NULL
               WHERE browser_run_id=? AND detail_status='EXTERNAL_BLOCKED' AND task_id IN
                 (SELECT task_id FROM browser_search_tasks WHERE browser_run_id=? AND status='queued')""",
            (run_id, run_id),
        )
        conn.execute(
            """UPDATE browser_platform_runs SET auth_status='unchecked',auth_reason=''
               WHERE browser_run_id=? AND platform IN
                 (SELECT DISTINCT platform FROM browser_search_tasks WHERE browser_run_id=? AND status='queued')""",
            (run_id, run_id),
        )
        conn.execute(
            """UPDATE browser_runs SET status='queued',completed_at=NULL,stop_requested=0,
                   stop_after_current=0,last_error='',last_progress_at=? WHERE browser_run_id=?""",
            (now, run_id),
        )
        message = "Run re-queued from durable checkpoints; completed details remain complete."
    else:
        raise ValueError(f"unsupported run control: {action}")
    conn.execute(
        "INSERT INTO browser_events(browser_run_id,event_at,event_type,message,payload_json) VALUES(?,?,?,?,?)",
        (run_id, now, f"dashboard_{action}", message, json.dumps({"action": action})),
    )
    conn.commit()
    refreshed = conn.execute("SELECT status FROM browser_runs WHERE browser_run_id=?", (run_id,)).fetchone()
    return {"ok": True, "run_id": run_id, "status": str(refreshed[0]), "action": action, "message": message}


def live_discoveries(conn: sqlite3.Connection, limit: int = 100, status: str = "") -> dict[str, Any]:
    status_clause = ""
    args: list[Any] = [max(1, min(500, int(limit)))]
    if status:
        status_clause = " WHERE r.detail_status=?"
        args.insert(0, status.upper())
    rows = conn.execute(
        """SELECT r.result_id,r.browser_run_id,r.task_id,r.source_site platform,r.source_job_id,
          r.title_hint,r.company_hint,r.location_hint,r.posted_text,r.posted_age_days,r.observed_at,
          r.detail_status,r.detail_attempts,r.detail_error,r.source_url,r.canonical_job_id,t.query_text
          FROM search_task_results r JOIN browser_search_tasks t ON t.task_id=r.task_id""" + status_clause + " ORDER BY r.result_id DESC LIMIT ?", args,
    ).fetchall()
    pending = int(conn.execute(
        "SELECT COUNT(*) FROM search_task_results WHERE detail_status IN ('PENDING','RUNNING','RETRYABLE','EXTERNAL_BLOCKED')"
    ).fetchone()[0])
    return {"pending": pending, "discoveries": [_dict(row) for row in rows]}


def query_jobs(conn: sqlite3.Connection, params: dict[str, list[str]]) -> dict[str, Any]:
    def one(name: str, default: str = "") -> str:
        return (params.get(name) or [default])[0].strip()
    page = max(1, int(one("page", "1") or 1))
    page_size = min(200, max(10, int(one("page_size", "50") or 50)))
    conditions = ["1=1"]
    args: list[Any] = []
    view = one("view")
    if view == "actionable":
        conditions.append("j.is_active=1 AND j.recommendation IN ('APPLY_NOW','APPLY_VOLUME','HIGH_VALUE_STRETCH')")
    elif view not in {"", "all"}:
        raise ValueError(f"unsupported jobs view: {view}")
    mappings = {
        "recommendation": "j.recommendation", "lane": "j.career_lane", "remote": "j.remote_gate",
        "employment": "j.employment_class", "resume": "j.resume_variant",
        "application_status": "upper(j.application_status)", "change_status": "j.change_status",
    }
    for key, column in mappings.items():
        value = one(key)
        if value:
            conditions.append(f"{column}=?")
            args.append(value.upper() if key == "application_status" else value)
    text = one("q")
    if text:
        conditions.append("(j.title LIKE ? OR j.company LIKE ? OR j.description LIKE ?)")
        args.extend([f"%{text}%"] * 3)
    source = one("source")
    if source:
        conditions.append("EXISTS(SELECT 1 FROM source_occurrences o WHERE o.job_id=j.job_id AND o.source_site=?)")
        args.append(source)
    salary = one("salary_min")
    if salary:
        conditions.append("COALESCE(j.salary_annual_mid,0)>=?")
        args.append(float(salary))
    min_score, max_score = one("min_score"), one("max_score")
    if min_score:
        conditions.append("COALESCE(j.door_score,0)>=?"); args.append(float(min_score))
    if max_score:
        conditions.append("COALESCE(j.door_score,0)<=?"); args.append(float(max_score))
    age = one("age_days")
    if age:
        conditions.append("j.posted_at>=datetime('now',?)")
        args.append(f"-{int(age)} days")
    where = " AND ".join(conditions)
    total = int(conn.execute(f"SELECT COUNT(*) FROM jobs j WHERE {where}", args).fetchone()[0])
    rows = conn.execute(f"""SELECT j.job_id,j.recommendation,j.title,j.company,j.career_lane,
      COALESCE((SELECT group_concat(DISTINCT source_site) FROM source_occurrences o WHERE o.job_id=j.job_id),'') sources,
      j.posted_at,j.first_seen,j.last_seen,j.salary_text,j.employment_class,j.remote_gate,j.eligible_states_json,j.location_raw,
      j.relevance_score,j.qualification_score,j.landing_score,j.career_score,j.door_score,
      j.resume_variant,j.application_status,j.change_status,j.description_state
      FROM jobs j WHERE {where} ORDER BY COALESCE(j.application_priority_score,j.door_score,0) DESC,j.last_seen DESC
      LIMIT ? OFFSET ?""", [*args, page_size, (page - 1) * page_size]).fetchall()
    return {"page": page, "page_size": page_size, "total": total, "columns": TABLE_COLUMNS, "jobs": [_dict(row) for row in rows]}


def job_detail(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if row is None:
        return None
    job = _dict(row) or {}
    for key in tuple(job):
        if key.endswith("_json"):
            try:
                job[key] = json.loads(job[key] or "[]")
            except (TypeError, json.JSONDecodeError):
                pass
    occurrences = [_dict(x) for x in conn.execute("SELECT * FROM source_occurrences WHERE job_id=? ORDER BY source_site,first_seen", (job_id,))]
    versions = [_dict(x) for x in conn.execute("SELECT * FROM job_versions WHERE job_id=? ORDER BY version_no DESC", (job_id,))]
    diffs = [_dict(x) for x in conn.execute("SELECT * FROM job_diffs WHERE job_id=? ORDER BY version_id DESC,field_name", (job_id,))]
    applications = [_dict(x) for x in conn.execute("SELECT * FROM application_events WHERE job_id=? ORDER BY event_id DESC", (job_id,))]
    verifications = [_dict(x) for x in conn.execute("SELECT * FROM source_verifications WHERE job_id=? ORDER BY verified_at DESC", (job_id,))]
    return {"job": job, "occurrences": occurrences, "versions": versions, "diffs": diffs, "applications": applications, "verifications": verifications}


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], bundle: ConfigBundle):
        super().__init__(address, DashboardHandler)
        self.bundle = bundle


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "JobBotDashboard/3.2.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    @property
    def bundle(self) -> ConfigBundle:
        return self.server.bundle  # type: ignore[attr-defined]

    def _conn(self) -> sqlite3.Connection:
        return Database(self.bundle).connect()

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, value: Any) -> None:
        self._send(code, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/":
            self._static("dashboard.html", "text/html; charset=utf-8")
            return
        if parsed.path in {"/static/dashboard.js", "/static/dashboard.css"}:
            name = parsed.path.rsplit("/", 1)[-1]
            content_type = "application/javascript; charset=utf-8" if name.endswith(".js") else "text/css; charset=utf-8"
            self._static(name, content_type)
            return
        conn = self._conn()
        try:
            if parsed.path == "/api/identity":
                self._json(200, identity(conn, self.bundle)); return
            if parsed.path == "/api/summary":
                self._json(200, summary(conn)); return
            if parsed.path == "/api/strategy":
                self._json(200, strategy_info(self.bundle)); return
            if parsed.path == "/api/coverage":
                self._json(200, coverage(conn)); return
            if parsed.path == "/api/run":
                self._json(200, active_run(conn)); return
            if parsed.path == "/api/discoveries":
                query = urllib.parse.parse_qs(parsed.query)
                limit = int((query.get("limit") or ["100"])[0])
                status = str((query.get("status") or [""])[0])
                self._json(200, live_discoveries(conn, limit, status)); return
            if parsed.path == "/api/jobs":
                self._json(200, query_jobs(conn, urllib.parse.parse_qs(parsed.query))); return
            if parsed.path.startswith("/api/jobs/"):
                job_id = urllib.parse.unquote(parsed.path.removeprefix("/api/jobs/"))
                detail = job_detail(conn, job_id)
                self._json(200 if detail else 404, detail or {"error": "not_found"}); return
            self._json(404, {"error": "not_found"})
        finally:
            conn.close()

    def _static(self, name: str, content_type: str) -> None:
        path = self.bundle.root / "src" / "jobbot" / "web" / name
        if not path.is_file():
            self._json(404, {"ok": False, "error": f"static asset missing: {name}"}); return
        self._send(200, path.read_bytes(), content_type)

    def do_POST(self) -> None:
        if not self.path.startswith("/api/"):
            self._json(404, {"error": "not_found"}); return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > 1024 * 1024:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(size).decode("utf-8"))
        except Exception as exc:
            self._json(400, {"error": str(exc)}); return
        conn = self._conn()
        try:
            if self.path == "/api/run/control":
                try:
                    action = str(payload.get("action", "")).strip().lower()
                    if action == "emergency_stop":
                        action = "emergency"
                    result = control_run(conn, action, payload.get("run_id"))
                    if action == "resume":
                        log_path = self.bundle.output_dir / "logs" / f"run_{result['run_id']}_dashboard_resume.log"
                        log_path.parent.mkdir(parents=True, exist_ok=True)
                        with log_path.open("ab") as log_handle:
                            subprocess.Popen(
                                [sys.executable, "-m", "jobbot", "resume", "--run-id", str(result["run_id"])],
                                cwd=self.bundle.root, stdout=log_handle, stderr=subprocess.STDOUT,
                                start_new_session=True,
                            )
                        result["launcher_started"] = True
                        result["message"] += " Browser resume launcher started; this dashboard will update from SQLite."
                    self._json(200, result)
                except (TypeError, ValueError) as exc:
                    self._json(400, {"ok": False, "error": str(exc)})
                return
            if self.path.startswith("/api/jobs/") and self.path.endswith("/application"):
                job_id = urllib.parse.unquote(self.path.removeprefix("/api/jobs/").removesuffix("/application"))
                try:
                    event = mark(conn, job_id, str(payload.get("status", "")), notes=str(payload.get("notes", "")), source="dashboard")
                    self._json(200, {"ok": True, "event": event.__dict__})
                except ApplicationError as exc:
                    self._json(400, {"ok": False, "error": str(exc)})
                return
            if self.path.startswith("/api/jobs/") and self.path.endswith("/note"):
                job_id = urllib.parse.unquote(self.path.removeprefix("/api/jobs/").removesuffix("/note"))
                try:
                    event = add_note(conn, job_id, str(payload.get("note", "")), source="dashboard")
                    self._json(200, {"ok": True, "event": event.__dict__})
                except ApplicationError as exc:
                    self._json(400, {"ok": False, "error": str(exc)})
                return
            if self.path == "/api/export-selected":
                ids = [str(x) for x in payload.get("job_ids", [])]
                path = export_selected(conn, self.bundle.output_dir, ids)
                self._json(200, {"ok": True, "path": str(path), "count": len(ids)}); return
            self._json(404, {"error": "not_found"})
        finally:
            conn.close()


def create_server(bundle: ConfigBundle, host: str | None = None, port: int | None = None) -> DashboardServer:
    Database(bundle).migrate()
    runtime = bundle.runtime["runtime"]
    selected_host = host or str(runtime["dashboard_host"])
    if selected_host != "127.0.0.1":
        raise ValueError("dashboard may bind only to 127.0.0.1")
    return DashboardServer((selected_host, int(runtime["dashboard_port"] if port is None else port)), bundle)


def serve(bundle: ConfigBundle, *, host: str | None = None, port: int | None = None, open_browser: bool = True) -> None:
    server = create_server(bundle, host, port)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(url, flush=True)
    if open_browser:
        def open_dashboard() -> None:
            if sys.platform == "darwin":
                subprocess.Popen(["open", "-g", "-a", "Google Chrome", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                webbrowser.open(url)
        threading.Timer(0.25, open_dashboard).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


DISCOVERY_COLUMNS = ("result_id", "platform", "title_hint", "company_hint", "location_hint", "posted_text", "detail_status", "detail_attempts", "observed_at")


DASHBOARD_HTML = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>JobBot v3.2.1 Dashboard</title><style>
:root{font-family:system-ui,-apple-system,sans-serif;color:#172033;background:#f4f6f9}body{margin:0}header{padding:18px 24px;background:#16213e;color:white}header h1{margin:0}.summary{display:grid;grid-template-columns:repeat(9,minmax(90px,1fr));gap:8px;padding:14px}.card,.filters,.detail{background:white;border:1px solid #dce2ea;border-radius:10px;padding:10px}.card b{display:block;font-size:22px}.filters{margin:0 14px 12px;display:flex;gap:8px;flex-wrap:wrap}.filters input,.filters select,button{padding:7px;border:1px solid #bbc4d1;border-radius:7px;background:white}.table-wrap{margin:0 14px;overflow:auto;max-height:58vh;background:white;border:1px solid #dce2ea}table{border-collapse:collapse;min-width:2200px;width:100%}th,td{font-size:12px;text-align:left;padding:7px;border-bottom:1px solid #e5e7eb}th{position:sticky;top:0;background:#edf2f7}tr:hover{background:#f3f7ff}.pager{margin:10px 14px}.detail{margin:14px;white-space:pre-wrap;max-height:45vh;overflow:auto}.actions{display:flex;gap:5px;flex-wrap:wrap;margin:8px 14px}@media(max-width:900px){.summary{grid-template-columns:repeat(3,1fr)}}
</style></head><body><header><h1>JobBot v3.2.1</h1><div>Permanent remote-career ledger · read-only discovery · human application control</div></header><section class="summary" id="summary"></section>
<pre class="detail" id="runState">No browser run yet.</pre><pre class="detail" id="coverage">Loading primary and supplemental source coverage…</pre>
<h2 class="actions">Live discoveries <small id="pendingCount"></small></h2><div class="table-wrap" style="max-height:240px"><table style="min-width:1200px"><thead><tr id="discoveryHead"></tr></thead><tbody id="discoveryBody"></tbody></table></div>
<form class="filters" id="filters"><input name="q" placeholder="Title, company, description"><select name="recommendation"><option value="">Recommendation</option><option>APPLY_NOW</option><option>APPLY_VOLUME</option><option>HIGH_VALUE_STRETCH</option><option>REVIEW</option><option>REVIEW_REMOTE</option><option>CONTRACT_REVIEW</option><option>FIXED_TERM_REVIEW</option><option>PART_TIME_REVIEW</option><option>BRIDGE_ONLY</option><option>LOW_PRIORITY</option><option>OUT_OF_SCOPE</option><option>SKIP_HARD_GATE</option></select><input name="lane" placeholder="Career lane"><input name="source" placeholder="Source"><select name="remote"><option value="">Remote status</option><option value="pass">pass</option><option value="review">review</option><option value="reject">reject</option></select><input name="employment" placeholder="Employment type"><input name="salary_min" type="number" placeholder="Salary min"><input name="age_days" type="number" placeholder="Posting age days"><input name="resume" placeholder="Resume"><select name="application_status"><option value="">Application status</option>''' + ''.join(f'<option>{x}</option>' for x in STATUSES) + r'''</select><select name="change_status"><option value="">New/updated</option><option>NEW</option><option>UPDATED</option><option>CLOSED</option><option>REOPENED</option></select><input name="min_score" type="number" placeholder="Min Door"><input name="max_score" type="number" placeholder="Max Door"><button>Filter</button></form>
<div class="actions"><button id="export" type="button">Export selected</button><span id="message"></span></div><div class="table-wrap"><table><thead><tr id="head"></tr></thead><tbody id="body"></tbody></table></div><div class="pager"><button id="prev">Previous</button> <span id="page"></span> <button id="next">Next</button></div><div class="actions" id="statusActions"></div><pre class="detail" id="detail">Select a job row to inspect full description, requirements, evidence, gaps, versions, diffs, sources, and application timeline.</pre>
<script>'use strict';let page=1,last=null,selectedJob='';const esc=s=>String(s??'');async function api(url,opt){const r=await fetch(url,opt);const x=await r.json();if(!r.ok)throw Error(x.error||r.status);return x}function params(){const p=new URLSearchParams(new FormData(document.querySelector('#filters')));p.set('page',page);p.set('page_size','50');return p}async function load(){const checked=new Set([...document.querySelectorAll('#body input:checked')].map(x=>x.dataset.id));last=await api('/api/jobs?'+params());head.innerHTML='<th>Select</th>'+last.columns.map(x=>'<th>'+esc(x)+'</th>').join('');body.replaceChildren();for(const j of last.jobs){const tr=document.createElement('tr');const check=document.createElement('input');check.type='checkbox';check.dataset.id=j.job_id;check.checked=checked.has(j.job_id);const td=document.createElement('td');td.append(check);tr.append(td);for(const c of last.columns){const cell=document.createElement('td');cell.textContent=esc(j[c]);tr.append(cell)}tr.onclick=e=>{if(e.target!==check)show(j.job_id)};body.append(tr)}document.querySelector('#page').textContent=`Page ${last.page} · ${last.total} jobs`;prev.disabled=page<=1;next.disabled=page*last.page_size>=last.total}async function cards(){const x=await api('/api/summary');summary.innerHTML=Object.entries(x).map(([k,v])=>`<div class="card"><span>${k}</span><b>${v}</b></div>`).join('')}async function sourceCoverage(){const x=await api('/api/coverage');const lines=['PRIMARY COVERAGE'];for(const [site,v] of Object.entries(x.primary))lines.push(`${site}: tasks ${v.total} · exhausted ${v.exhausted} · incomplete ${v.incomplete} · challenged ${v.challenged} · auth ${v.auth_required} · deferred ${v.deferred} · failed ${v.failed} · results ${v.results} · details ${v.details} · unique ${v.unique_jobs}`);lines.push('',`SUPPLEMENTAL: ${Object.entries(x.supplemental).map(([k,v])=>k+' '+v).join(' · ')||'none recorded'}`);coverage.textContent=lines.join('\n')}async function runProgress(){const x=await api('/api/run');if(!x.run){runState.textContent='No browser run yet.';return}const r=x.run,lines=[`RUN #${r.browser_run_id} · ${r.status} · last progress ${r.last_progress_at||'never'}`];if(x.current_task)lines.push(`ACTIVE ${x.current_task.platform} · ${x.current_task.query_text} · page ${x.current_task.page_number} · results ${x.current_task.results_seen} · details ${x.current_task.detail_count_read}`);for(const p of x.platforms)lines.push(`${p.platform}: auth=${p.auth_status} running=${p.running} exhausted=${p.tasks_completed}/${p.tasks_total} deferred=${p.deferred} discoveries=${p.discoveries} details=${p.details_complete}`);runState.textContent=lines.join('\n')}async function discoveries(){const x=await api('/api/discoveries?limit=100'),cols=''' + json.dumps(DISCOVERY_COLUMNS) + r''';pendingCount.textContent=`(${x.pending} pending)`;discoveryHead.innerHTML=cols.map(c=>'<th>'+esc(c)+'</th>').join('');discoveryBody.replaceChildren();for(const d of x.discoveries){const tr=document.createElement('tr');for(const c of cols){const td=document.createElement('td');td.textContent=esc(d[c]);tr.append(td)}tr.onclick=()=>{if(d.source_url)window.open(d.source_url,'_blank','noopener')};discoveryBody.append(tr)}}async function show(id){selectedJob=id;const x=await api('/api/jobs/'+encodeURIComponent(id));detail.textContent=JSON.stringify(x,null,2);statusActions.innerHTML='';for(const s of ''' + json.dumps(STATUSES) + r'''){const b=document.createElement('button');b.textContent=s;b.onclick=async()=>{const notes=prompt('Optional note')||'';await api('/api/jobs/'+encodeURIComponent(id)+'/application',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:s,notes})});await Promise.all([load(),cards(),show(id)])};statusActions.append(b)}}filters.onsubmit=e=>{e.preventDefault();page=1;load()};prev.onclick=()=>{page--;load()};next.onclick=()=>{page++;load()};export.onclick=async()=>{const ids=[...document.querySelectorAll('#body input:checked')].map(x=>x.dataset.id);const x=await api('/api/export-selected',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_ids:ids})});message.textContent=`Exported ${x.count} to ${x.path}`};async function refresh(){await Promise.all([cards(),sourceCoverage(),runProgress(),discoveries(),load()])}refresh();setInterval(refresh,10000);</script></body></html>'''
