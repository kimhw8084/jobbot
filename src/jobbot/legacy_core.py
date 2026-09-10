#!/usr/bin/env python3
"""Remote Career Job Search Automation.

A local, deterministic job-search engine optimized for:
1) fastest realistic fully-remote offer now, and
2) strong long-term career capital in regulated healthcare operations/data/quality.

Core run uses Python 3.11+ standard library only.
Legacy user-assisted capture may use Playwright for supplemental sources only.
The LinkedIn/Indeed/Glassdoor traversal is implemented exclusively by the
normal-Chrome MV3 extension and loopback bridge.

The program does NOT bypass CAPTCHAs, access controls, login challenges, or anti-bot systems.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
from html.parser import HTMLParser
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

try:
    import tomllib
except ModuleNotFoundError:
    raise SystemExit("Python 3.11+ is required.")

VERSION = "1.1.0"
RESTRICTED_AUTOMATION_DOMAINS = ("linkedin.com", "indeed.com", "glassdoor.com")
STOPWORDS = {
    "and", "or", "the", "a", "an", "of", "for", "to", "in", "with", "remote",
    "healthcare", "health", "specialist", "coordinator", "associate", "analyst",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utcnow().isoformat(timespec="seconds")


def clean_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple, set)):
        return " | ".join(clean_text(x) for x in v if clean_text(x))
    return re.sub(r"\s+", " ", str(v)).strip()


class HTMLTextExtractor(HTMLParser):
    BLOCK_TAGS = {
        "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
        "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6",
        "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table",
        "td", "th", "tr", "ul",
    }

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())


def strip_html(v: Any) -> str:
    s = clean_text(v)
    if not s:
        return ""
    p = HTMLTextExtractor()
    try:
        p.feed(s)
        text = " ".join(p.parts)
        text = re.sub(r"[ \t\f\v]+", " ", text)
        text = re.sub(r" *\n *", "\n", text)
        text = re.sub(r"\n{2,}", "\n", text)
        return text.strip()
    except Exception:
        return clean_text(re.sub(r"<[^>]+>", " ", s))


def norm(v: Any) -> str:
    s = strip_html(v).lower()
    s = s.replace("&", " and ")
    return re.sub(r"[^a-z0-9+#/]+", " ", s).strip()


def slug(v: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", v.lower()).strip("-") or "item"


def host_of(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).netloc.lower().split(":")[0]
    except Exception:
        return ""


def canonical_url(url: str) -> str:
    if not url:
        return ""
    try:
        raw = validate_web_url(url, require_https=False)
        p = urllib.parse.urlsplit(raw)
        drop = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "ref", "src", "source", "gh_src", "trk", "trackingid"}
        q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True) if k.lower() not in drop and not k.lower().startswith("utm_")]
        return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), re.sub(r"/+", "/", p.path), urllib.parse.urlencode(q), ""))
    except Exception:
        return ""


def is_restricted_url(url: str) -> bool:
    h = host_of(url)
    return any(h == d or h.endswith("." + d) for d in RESTRICTED_AUTOMATION_DOMAINS)




BLOCKED_HOST_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".home.arpa")

def is_blocked_host(host: str) -> bool:
    h=(host or "").strip().lower().rstrip(".")
    if not h:
        return True
    if h in {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}:
        return True
    if any(h.endswith(sfx) for sfx in BLOCKED_HOST_SUFFIXES):
        return True
    try:
        ip=ipaddress.ip_address(h.strip("[]"))
        return bool(ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified)
    except ValueError:
        return False

def validate_web_url(url: str, *, require_https: bool=False) -> str:
    """Validate untrusted/configured URLs before browser or HTTP use. Fail closed on local/private targets."""
    raw=clean_text(url)
    if not raw:
        raise ValueError("empty URL")
    try:
        p=urllib.parse.urlsplit(raw)
    except Exception as e:
        raise ValueError(f"invalid URL: {e}")
    if p.scheme not in ({"https"} if require_https else {"http","https"}):
        raise ValueError(f"unsupported URL scheme: {p.scheme or '(none)'}")
    host=(p.hostname or "").lower()
    if is_blocked_host(host):
        raise ValueError(f"local/private host is blocked: {host or '(missing host)'}")
    if p.username or p.password:
        raise ValueError("URLs containing embedded credentials are blocked")
    return raw

def safe_output_url(url: str) -> str:
    try:
        return validate_web_url(url, require_https=False)
    except Exception:
        return ""

def csv_safe_cell(v: Any) -> Any:
    """Prevent spreadsheet formula injection from untrusted job-board text."""
    if not isinstance(v,str):
        return v
    if v and (v[0] in "=+-@" or v[0] in "\t\r"):
        return "'"+v
    return v


def load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as f:
        return tomllib.load(f)


def abs_path(base: Path, v: str) -> Path:
    p = Path(v)
    return p if p.is_absolute() else (base / p)


def parse_dt(v: Any) -> Optional[datetime]:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(float(v), tz=timezone.utc)
        except Exception:
            return None
    s = clean_text(v)
    low = s.lower()
    now = utcnow()
    if low in {"today", "just posted", "just now", "new"}:
        return now
    m = re.search(r"(\d+)\s*(minute|hour|day|week|month)s?\s+ago", low)
    if m:
        n = int(m.group(1)); unit = m.group(2)
        delta = {"minute": timedelta(minutes=n), "hour": timedelta(hours=n), "day": timedelta(days=n), "week": timedelta(weeks=n), "month": timedelta(days=30*n)}[unit]
        return now - delta
    s2 = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d", "%a, %d %b %Y %H:%M:%S %z", "%B %d, %Y", "%b %d, %Y"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
    return None


def posted_age_hours(v: Any) -> Optional[float]:
    dt = parse_dt(v)
    if not dt:
        return None
    return max(0.0, (utcnow() - dt).total_seconds() / 3600)


def salary_number(s: str) -> Optional[float]:
    s = s.replace(",", "").strip().lower()
    m = re.search(r"\$?\s*(\d+(?:\.\d+)?)\s*([km]?)", s)
    if not m:
        return None
    n = float(m.group(1))
    if m.group(2) == "k": n *= 1000
    if m.group(2) == "m": n *= 1_000_000
    return n


def parse_salary(text: Any, explicit_min: Any = None, explicit_max: Any = None, currency: str = "USD", period: str = "") -> tuple[Optional[float], Optional[float], str, str]:
    try:
        mn = float(explicit_min) if explicit_min not in (None, "") else None
    except Exception:
        mn = salary_number(str(explicit_min))
    try:
        mx = float(explicit_max) if explicit_max not in (None, "") else None
    except Exception:
        mx = salary_number(str(explicit_max))
    s = clean_text(text)
    if mn is None and mx is None and s:
        vals = [salary_number(x.group(0)) for x in re.finditer(r"\$?\s*\d[\d,]*(?:\.\d+)?\s*[kKmM]?", s)]
        vals = [x for x in vals if x is not None]
        if vals:
            mn = min(vals); mx = max(vals) if len(vals) > 1 else vals[0]
    plow = (period or s).lower()
    if not period:
        if any(x in plow for x in ("/hr", "hour", "hourly")): period = "hour"
        elif any(x in plow for x in ("/month", "monthly", "per month")): period = "month"
        else: period = "year"
    return mn, mx, currency or "USD", period


def annualized_salary(mn: Optional[float], mx: Optional[float], period: str) -> Optional[float]:
    vals = [x for x in (mn, mx) if x is not None]
    if not vals: return None
    mid = sum(vals) / len(vals)
    p = (period or "year").lower()
    if "hour" in p: return mid * 2080
    if "month" in p: return mid * 12
    if "week" in p: return mid * 52
    if "day" in p: return mid * 260
    return mid


def significant_tokens(s: str) -> set[str]:
    return {t for t in norm(s).split() if len(t) >= 3 and t not in STOPWORDS}


def fuzzy_keyword_match(keyword: str, title: str, description: str) -> float:
    k = norm(keyword); t = norm(title); d = norm(description)
    if not k: return 0.0
    if k in t: return 1.0
    kt = significant_tokens(k); tt = significant_tokens(t)
    if kt:
        overlap = len(kt & tt) / len(kt)
        if overlap >= 0.66: return 0.82 + min(0.15, overlap * 0.15)
        combined = significant_tokens(t + " " + d[:5000])
        cov = len(kt & combined) / len(kt)
        if cov >= 0.85: return 0.68
    return 0.0


def detect_explicit_required_credential(text: str, cred: str) -> bool:
    # Require proximity to requirement language to avoid rejecting mere mentions.
    c = re.escape(cred.lower())
    low = text.lower()
    patterns = [
        rf"\b{c}\b.{{0,45}}\b(required|must|mandatory|active|current|license)\b",
        rf"\b(required|must have|must possess|active|current)\b.{{0,45}}\b{c}\b",
    ]
    return any(re.search(p, low, flags=re.I | re.S) for p in patterns)


def years_required(text: str) -> Optional[int]:
    low = text.lower()
    vals: list[int] = []
    for m in re.finditer(r"(?:minimum of\s*)?(\d{1,2})(?:\+)?\s*(?:-|to\s*\d{1,2}\s*)?years?\s+(?:of\s+)?(?:relevant\s+)?experience", low):
        vals.append(int(m.group(1)))
    return max(vals) if vals else None


def extract_states(text: str) -> set[str]:
    # Conservative detection used only for remote-state restrictions.
    states = {
        "AL":"alabama","AK":"alaska","AZ":"arizona","AR":"arkansas","CA":"california","CO":"colorado","CT":"connecticut","DE":"delaware","FL":"florida","GA":"georgia","HI":"hawaii","ID":"idaho","IL":"illinois","IN":"indiana","IA":"iowa","KS":"kansas","KY":"kentucky","LA":"louisiana","ME":"maine","MD":"maryland","MA":"massachusetts","MI":"michigan","MN":"minnesota","MS":"mississippi","MO":"missouri","MT":"montana","NE":"nebraska","NV":"nevada","NH":"new hampshire","NJ":"new jersey","NM":"new mexico","NY":"new york","NC":"north carolina","ND":"north dakota","OH":"ohio","OK":"oklahoma","OR":"oregon","PA":"pennsylvania","RI":"rhode island","SC":"south carolina","SD":"south dakota","TN":"tennessee","TX":"texas","UT":"utah","VT":"vermont","VA":"virginia","WA":"washington","WV":"west virginia","WI":"wisconsin","WY":"wyoming",
    }
    low = " " + norm(text) + " "
    found: set[str] = set()
    for abbr, name in states.items():
        if f" {name} " in low or re.search(rf"\b{abbr}\b", text):
            found.add(abbr)
    return found


def canonical_job_id(company: str, title: str, location: str, source_id: str = "") -> str:
    # Cross-source merging is decided in PrecisionStore.resolve_job_id(), not
    # by dropping distinct source requisition IDs from the fallback key.
    core = "|".join([norm(company), norm(title), norm(location)])
    if source_id or not norm(company) or not norm(title):
        core += "|" + norm(source_id)
    return "J" + hashlib.sha256(core.encode("utf-8", errors="ignore")).hexdigest()[:14].upper()


@dataclass
class Job:
    source_site: str
    source_job_id: str = ""
    canonical_url: str = ""
    apply_url: str = ""
    title: str = ""
    company: str = ""
    location_raw: str = ""
    remote_status: str = "remote"
    employment_type: str = ""
    salary_text: str = ""
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    salary_currency: str = "USD"
    salary_period: str = "year"
    posted_at: str = ""
    description: str = ""
    category: str = ""
    tags: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    # Strategy-derived
    search_profile: str = ""
    career_lane: str = ""
    resume_variant: str = ""
    matched_keywords: list[str] = field(default_factory=list)
    remote_gate: str = "review"
    remote_gate_reason: str = ""
    hard_reject_reasons: list[str] = field(default_factory=list)
    matched_evidence: list[str] = field(default_factory=list)
    matched_positive: list[str] = field(default_factory=list)
    matched_accelerators: list[str] = field(default_factory=list)
    matched_bilingual: list[str] = field(default_factory=list)
    years_required: Optional[int] = None
    landing_score: float = 0.0
    career_score: float = 0.0
    door_score: float = 0.0
    recommendation: str = "REVIEW"
    score_reasons: list[str] = field(default_factory=list)

    @property
    def job_id(self) -> str:
        return canonical_job_id(self.company, self.title, self.location_raw, self.source_job_id)


class CommitControlledConnection(sqlite3.Connection):
    """SQLite connection whose commits can be deferred by one owner.

    The bridge uses this only while a durable RPC receipt and its business
    mutation share one transaction. Normal stores retain ordinary commit
    semantics.
    """

    defer_commits = False

    def commit(self) -> None:
        if self.defer_commits:
            return
        super().commit()

    def durable_commit(self) -> None:
        super().commit()


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, factory=CommitControlledConnection)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.execute("PRAGMA synchronous=FULL")
        self._schema()

    def _schema(self) -> None:
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
          job_id TEXT PRIMARY KEY,
          title TEXT, company TEXT, location_raw TEXT,
          canonical_url TEXT, apply_url TEXT,
          remote_status TEXT, employment_type TEXT,
          salary_text TEXT, salary_min REAL, salary_max REAL, salary_currency TEXT, salary_period TEXT,
          posted_at TEXT, description TEXT, category TEXT, tags_json TEXT,
          search_profile TEXT, career_lane TEXT, resume_variant TEXT,
          matched_keywords_json TEXT, remote_gate TEXT, remote_gate_reason TEXT,
          hard_reject_reasons_json TEXT, matched_evidence_json TEXT,
          matched_positive_json TEXT, matched_accelerators_json TEXT, matched_bilingual_json TEXT,
          years_required INTEGER,
          landing_score REAL, career_score REAL, door_score REAL, recommendation TEXT,
          score_reasons_json TEXT,
          first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, seen_count INTEGER NOT NULL DEFAULT 1,
          application_status TEXT NOT NULL DEFAULT 'new', notes TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_rec ON jobs(recommendation, door_score DESC);
        CREATE INDEX IF NOT EXISTS idx_jobs_lane ON jobs(career_lane);
        CREATE TABLE IF NOT EXISTS source_occurrences (
          occurrence_key TEXT PRIMARY KEY,
          job_id TEXT NOT NULL,
          source_site TEXT NOT NULL,
          source_job_id TEXT,
          source_url TEXT,
          apply_url TEXT,
          raw_json TEXT,
          first_seen TEXT NOT NULL,
          last_seen TEXT NOT NULL,
          seen_count INTEGER NOT NULL DEFAULT 1,
          FOREIGN KEY(job_id) REFERENCES jobs(job_id)
        );
        CREATE TABLE IF NOT EXISTS runs (
          run_id INTEGER PRIMARY KEY AUTOINCREMENT,
          started_at TEXT, finished_at TEXT, mode TEXT,
          source_status_json TEXT, new_jobs INTEGER, updated_jobs INTEGER
        );
        """)
        self.conn.commit()

    def upsert(self, job: Job) -> bool:
        now = now_iso()
        jid = job.job_id
        exists = self.conn.execute("SELECT 1 FROM jobs WHERE job_id=?", (jid,)).fetchone() is not None
        vals = {
            "job_id": jid, "title": job.title, "company": job.company, "location_raw": job.location_raw,
            "canonical_url": job.canonical_url, "apply_url": job.apply_url,
            "remote_status": job.remote_status, "employment_type": job.employment_type,
            "salary_text": job.salary_text, "salary_min": job.salary_min, "salary_max": job.salary_max,
            "salary_currency": job.salary_currency, "salary_period": job.salary_period,
            "posted_at": job.posted_at, "description": job.description, "category": job.category,
            "tags_json": json.dumps(job.tags, ensure_ascii=False), "search_profile": job.search_profile,
            "career_lane": job.career_lane, "resume_variant": job.resume_variant,
            "matched_keywords_json": json.dumps(job.matched_keywords, ensure_ascii=False),
            "remote_gate": job.remote_gate, "remote_gate_reason": job.remote_gate_reason,
            "hard_reject_reasons_json": json.dumps(job.hard_reject_reasons, ensure_ascii=False),
            "matched_evidence_json": json.dumps(job.matched_evidence, ensure_ascii=False),
            "matched_positive_json": json.dumps(job.matched_positive, ensure_ascii=False),
            "matched_accelerators_json": json.dumps(job.matched_accelerators, ensure_ascii=False),
            "matched_bilingual_json": json.dumps(job.matched_bilingual, ensure_ascii=False),
            "years_required": job.years_required, "landing_score": job.landing_score,
            "career_score": job.career_score, "door_score": job.door_score,
            "recommendation": job.recommendation, "score_reasons_json": json.dumps(job.score_reasons, ensure_ascii=False),
        }
        if exists:
            sets = ",".join(f"{k}=?" for k in vals if k != "job_id")
            args = [vals[k] for k in vals if k != "job_id"] + [now, jid]
            self.conn.execute(f"UPDATE jobs SET {sets}, last_seen=?, seen_count=seen_count+1 WHERE job_id=?", args)
        else:
            cols = list(vals) + ["first_seen", "last_seen"]
            args = [vals[k] for k in vals] + [now, now]
            self.conn.execute(f"INSERT INTO jobs ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})", args)
        occ_raw = f"{job.source_site}|{job.source_job_id or canonical_url(job.canonical_url or job.apply_url)}"
        ok = "O" + hashlib.sha256(occ_raw.encode()).hexdigest()[:18].upper()
        row = self.conn.execute("SELECT 1 FROM source_occurrences WHERE occurrence_key=?", (ok,)).fetchone()
        if row:
            self.conn.execute("UPDATE source_occurrences SET last_seen=?, seen_count=seen_count+1, raw_json=? WHERE occurrence_key=?", (now, json.dumps(job.raw, ensure_ascii=False), ok))
        else:
            self.conn.execute("INSERT INTO source_occurrences VALUES (?,?,?,?,?,?,?,?,?,?)", (
                ok, jid, job.source_site, job.source_job_id, job.canonical_url, job.apply_url,
                json.dumps(job.raw, ensure_ascii=False), now, now, 1
            ))
        self.conn.commit()
        return not exists

    def rows(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM jobs ORDER BY door_score DESC, last_seen DESC"))

    def source_occurrences(self, job_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM source_occurrences WHERE job_id=? ORDER BY source_site", (job_id,)))

    def mark(self, job_id: str, status: str, notes: str = "") -> None:
        self.conn.execute("UPDATE jobs SET application_status=?, notes=CASE WHEN ?='' THEN notes ELSE ? END WHERE job_id=?", (status, notes, notes, job_id))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


class HttpClient:
    def __init__(self, cache_dir: Path, cache_minutes: int, timeout: int, user_agent: str, attempts: int = 2) -> None:
        self.cache_dir = cache_dir; cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_minutes = max(0, cache_minutes); self.timeout = timeout; self.user_agent = user_agent; self.attempts = max(1, min(3, int(attempts)) )

    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / (hashlib.sha256(url.encode()).hexdigest() + ".cache")

    def get_bytes(self, url: str, accept: str = "application/json", allow_stale: bool = True) -> bytes:
        url = validate_web_url(url, require_https=True)
        cp = self._cache_path(url)
        if self.cache_minutes > 0 and cp.exists() and time.time() - cp.stat().st_mtime < self.cache_minutes * 60:
            return cp.read_bytes()
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent, "Accept": accept})
        last: Exception | None = None
        max_bytes = 20 * 1024 * 1024
        for attempt in range(self.attempts):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    final_url = validate_web_url(r.geturl(), require_https=True)
                    if host_of(final_url) != host_of(url):
                        # Cross-host redirects are allowed only when they remain public HTTPS.
                        validate_web_url(final_url, require_https=True)
                    clen = clean_text(r.headers.get("Content-Length"))
                    if clen.isdigit() and int(clen) > max_bytes:
                        raise RuntimeError("response exceeds 20 MiB safety limit")
                    data = r.read(max_bytes + 1)
                    if len(data) > max_bytes:
                        raise RuntimeError("response exceeds 20 MiB safety limit")
                cp.write_bytes(data)
                return data
            except Exception as e:
                last = e
                time.sleep(1.5 * (attempt + 1))
        if allow_stale and cp.exists():
            print(f"  ! network failed for {host_of(url)}; using cached copy", file=sys.stderr)
            return cp.read_bytes()
        # Preserve the typed root cause for supplemental isolation.  Wrapping
        # every network failure in RuntimeError made timeouts and HTTP 429s
        # indistinguishable from JobBot programming defects.
        if last is not None:
            raise last
        raise RuntimeError(f"GET failed for {host_of(url)}")

    def json(self, url: str) -> Any:
        return json.loads(self.get_bytes(url, "application/json").decode("utf-8", errors="replace"))


# ---------- Source normalizers ----------

def fetch_remotive(client: HttpClient, cfg: dict[str, Any]) -> list[Job]:
    obj = client.json(cfg["url"])
    rows = obj.get("jobs", []) if isinstance(obj, dict) else []
    out: list[Job] = []
    for x in rows:
        mn, mx, cur, per = parse_salary(x.get("salary"))
        out.append(Job(
            source_site="remotive", source_job_id=str(x.get("id", "")), canonical_url=canonical_url(x.get("url", "")), apply_url=canonical_url(x.get("url", "")),
            title=clean_text(x.get("title")), company=clean_text(x.get("company_name")), location_raw=clean_text(x.get("candidate_required_location") or "Remote"),
            remote_status="remote", employment_type=clean_text(x.get("job_type")), salary_text=clean_text(x.get("salary")), salary_min=mn, salary_max=mx, salary_currency=cur, salary_period=per,
            posted_at=clean_text(x.get("publication_date")), description=strip_html(x.get("description")), category=clean_text(x.get("category")), raw=x,
        ))
    return out


def fetch_jobicy(client: HttpClient, cfg: dict[str, Any]) -> list[Job]:
    obj = client.json(cfg["url"])
    rows = obj.get("jobs", []) if isinstance(obj, dict) else (obj if isinstance(obj, list) else [])
    out: list[Job] = []
    for x in rows:
        st = clean_text(x.get("annualSalaryMin") or x.get("salaryMin"))
        sx = clean_text(x.get("annualSalaryMax") or x.get("salaryMax"))
        saltxt = clean_text(x.get("salary") or (f"{st} - {sx}" if st or sx else ""))
        mn, mx, cur, per = parse_salary(saltxt, x.get("annualSalaryMin") or x.get("salaryMin"), x.get("annualSalaryMax") or x.get("salaryMax"), clean_text(x.get("salaryCurrency") or "USD"), "year")
        out.append(Job(
            source_site="jobicy", source_job_id=clean_text(x.get("id") or x.get("jobId") or x.get("jobSlug")), canonical_url=canonical_url(clean_text(x.get("url") or x.get("jobUrl"))), apply_url=canonical_url(clean_text(x.get("url") or x.get("jobUrl"))),
            title=clean_text(x.get("jobTitle") or x.get("title")), company=clean_text(x.get("companyName") or x.get("company")), location_raw=clean_text(x.get("jobGeo") or x.get("location") or "Remote"),
            remote_status="remote", employment_type=clean_text(x.get("jobType") or x.get("type")), salary_text=saltxt, salary_min=mn, salary_max=mx, salary_currency=cur, salary_period=per,
            posted_at=clean_text(x.get("pubDate") or x.get("publicationDate") or x.get("date")), description=strip_html(x.get("jobDescription") or x.get("jobExcerpt") or x.get("description")), category=clean_text(x.get("jobIndustry") or x.get("industry")), raw=x,
        ))
    return out


def fetch_remoteok(client: HttpClient, cfg: dict[str, Any]) -> list[Job]:
    obj = client.json(cfg["url"])
    rows = obj if isinstance(obj, list) else []
    out: list[Job] = []
    for x in rows:
        if not isinstance(x, dict) or not (x.get("position") or x.get("title")):
            continue
        mn, mx, cur, per = parse_salary("", x.get("salary_min"), x.get("salary_max"), "USD", "year")
        out.append(Job(
            source_site="remoteok", source_job_id=clean_text(x.get("id")), canonical_url=canonical_url(clean_text(x.get("url"))), apply_url=canonical_url(clean_text(x.get("apply_url") or x.get("url"))),
            title=clean_text(x.get("position") or x.get("title")), company=clean_text(x.get("company")), location_raw=clean_text(x.get("location") or "Remote"), remote_status="remote",
            employment_type=clean_text(x.get("type") or ""), salary_text=clean_text(x.get("salary")), salary_min=mn, salary_max=mx, salary_currency=cur, salary_period=per,
            posted_at=clean_text(x.get("date") or x.get("epoch")), description=strip_html(x.get("description")), category="", tags=[clean_text(t) for t in x.get("tags", []) if clean_text(t)], raw=x,
        ))
    return out


def fetch_remotelanders(client: HttpClient, cfg: dict[str, Any]) -> list[Job]:
    base = cfg["url"]; pages = int(cfg.get("pages", 5)); size = min(100, int(cfg.get("page_size", 100)))
    out: list[Job] = []
    for page in range(1, pages + 1):
        sep = "&" if "?" in base else "?"
        obj = client.json(f"{base}{sep}limit={size}&page={page}")
        rows = obj.get("jobs", []) if isinstance(obj, dict) else []
        if not rows: break
        for x in rows:
            mn, mx, cur, per = parse_salary(x.get("salary"))
            out.append(Job(
                source_site="remotelanders", source_job_id=clean_text(x.get("slug")), canonical_url=canonical_url(clean_text(x.get("url"))), apply_url=canonical_url(clean_text(x.get("applyUrl") or x.get("url"))),
                title=clean_text(x.get("title")), company=clean_text(x.get("company")), location_raw=clean_text(x.get("location") or "Remote"), remote_status="remote",
                employment_type=clean_text(x.get("type")), salary_text=clean_text(x.get("salary")), salary_min=mn, salary_max=mx, salary_currency=cur, salary_period=per,
                posted_at=clean_text(x.get("postedDate")), description="", category=clean_text(x.get("category")), tags=[clean_text(t) for t in x.get("subtags", [])], raw=x,
            ))
        if len(rows) < size: break
    return out


def fetch_direct_ats_watch(client: HttpClient, app_cfg: dict[str, Any]) -> list[Job]:
    out: list[Job] = []
    watch = app_cfg.get("ats_watch", {})
    # Optional future extension; config can add [[ats_watch.greenhouse]], etc.
    for x in watch.get("greenhouse", []) if isinstance(watch.get("greenhouse", []), list) else []:
        if not x.get("enabled", True): continue
        board = x.get("board", ""); company = x.get("company", board)
        if not board: continue
        obj = client.json(f"https://boards-api.greenhouse.io/v1/boards/{urllib.parse.quote(board)}/jobs?content=true")
        for j in obj.get("jobs", []):
            loc = clean_text((j.get("location") or {}).get("name"))
            out.append(Job(source_site="greenhouse", source_job_id=str(j.get("id", "")), canonical_url=canonical_url(j.get("absolute_url", "")), apply_url=canonical_url(j.get("absolute_url", "")), title=clean_text(j.get("title")), company=company, location_raw=loc, remote_status="remote" if "remote" in norm(loc + " " + j.get("title", "")) else "unknown", posted_at=clean_text(j.get("updated_at")), description=strip_html(j.get("content")), raw=j))
    for x in watch.get("lever", []) if isinstance(watch.get("lever", []), list) else []:
        if not x.get("enabled", True): continue
        site = x.get("site", ""); company = x.get("company", site)
        if not site: continue
        obj = client.json(f"https://api.lever.co/v0/postings/{urllib.parse.quote(site)}?mode=json")
        for j in obj if isinstance(obj, list) else []:
            cat = j.get("categories") or {}; loc = clean_text(cat.get("location")); sal = j.get("salaryRange") or {}
            out.append(Job(source_site="lever", source_job_id=clean_text(j.get("id")), canonical_url=canonical_url(j.get("hostedUrl", "")), apply_url=canonical_url(j.get("applyUrl", "")), title=clean_text(j.get("text")), company=company, location_raw=loc, remote_status="remote" if clean_text(j.get("workplaceType")).lower()=="remote" or "remote" in norm(loc) else clean_text(j.get("workplaceType")) or "unknown", employment_type=clean_text(cat.get("commitment")), salary_text=clean_text(j.get("salaryDescription")), salary_min=sal.get("min"), salary_max=sal.get("max"), salary_currency=clean_text(sal.get("currency") or "USD"), salary_period=clean_text(sal.get("interval") or "year"), description=strip_html(j.get("descriptionPlain") or j.get("description")), category=clean_text(cat.get("team") or cat.get("department")), raw=j))
    for x in watch.get("ashby", []) if isinstance(watch.get("ashby", []), list) else []:
        if not x.get("enabled", True): continue
        board = x.get("board", ""); company = x.get("company", board)
        if not board: continue
        obj = client.json(f"https://api.ashbyhq.com/posting-api/job-board/{urllib.parse.quote(board)}?includeCompensation=true")
        for j in obj.get("jobs", []):
            comp = j.get("compensation") or {}; stext = clean_text(comp.get("compensationTierSummary") or comp.get("scrapeableCompensationSalarySummary"))
            mn,mx,cur,per=parse_salary(stext)
            out.append(Job(source_site="ashby", source_job_id=clean_text(j.get("id") or j.get("jobUrl")), canonical_url=canonical_url(j.get("jobUrl", "")), apply_url=canonical_url(j.get("applyUrl", "")), title=clean_text(j.get("title")), company=company, location_raw=clean_text(j.get("location")), remote_status="remote" if "remote" in norm(j.get("workplaceType") or j.get("location")) else clean_text(j.get("workplaceType")) or "unknown", employment_type=clean_text(j.get("employmentType")), salary_text=stext, salary_min=mn, salary_max=mx, salary_currency=cur, salary_period=per, posted_at=clean_text(j.get("publishedAt")), description=strip_html(j.get("descriptionPlain") or j.get("descriptionHtml")), category=clean_text(j.get("department") or j.get("team")), raw=j))
    return out


# ---------- Public ATS enrichment ----------

def enrich_public_ats(client: HttpClient, job: Job) -> Job:
    url = job.apply_url or job.canonical_url
    h = host_of(url)
    try:
        if h.endswith("jobs.lever.co"):
            parts = [p for p in urllib.parse.urlsplit(url).path.split("/") if p]
            if len(parts) >= 2:
                site, pid = parts[0], parts[1]
                j = client.json(f"https://api.lever.co/v0/postings/{urllib.parse.quote(site)}/{urllib.parse.quote(pid)}?mode=json")
                if isinstance(j, dict):
                    cat = j.get("categories") or {}; sal = j.get("salaryRange") or {}
                    job.description = strip_html(j.get("descriptionPlain") or j.get("description")) or job.description
                    job.location_raw = clean_text(cat.get("location")) or job.location_raw
                    job.employment_type = clean_text(cat.get("commitment")) or job.employment_type
                    job.remote_status = "remote" if clean_text(j.get("workplaceType")).lower()=="remote" else (job.remote_status or clean_text(j.get("workplaceType")))
                    if sal:
                        job.salary_min = sal.get("min") or job.salary_min; job.salary_max = sal.get("max") or job.salary_max; job.salary_currency = clean_text(sal.get("currency") or job.salary_currency); job.salary_period = clean_text(sal.get("interval") or job.salary_period)
                    job.canonical_url = canonical_url(j.get("hostedUrl") or job.canonical_url); job.apply_url = canonical_url(j.get("applyUrl") or job.apply_url); job.raw["ats_enrichment"] = j
        elif "greenhouse.io" in h:
            parts = [p for p in urllib.parse.urlsplit(url).path.split("/") if p]
            board = ""; jid = ""
            if "jobs" in parts:
                idx = parts.index("jobs")
                if idx >= 1 and idx + 1 < len(parts): board, jid = parts[idx-1], re.sub(r"\D", "", parts[idx+1])
            if not jid:
                q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query); jid = clean_text((q.get("gh_jid") or [""])[0])
                if parts: board = parts[0]
            if board and jid:
                j = client.json(f"https://boards-api.greenhouse.io/v1/boards/{urllib.parse.quote(board)}/jobs/{urllib.parse.quote(jid)}?questions=true&pay_transparency=true")
                if isinstance(j, dict):
                    job.description = strip_html(j.get("content")) or job.description
                    job.title = clean_text(j.get("title")) or job.title
                    job.location_raw = clean_text((j.get("location") or {}).get("name")) or job.location_raw
                    job.posted_at = clean_text(j.get("updated_at")) or job.posted_at
                    job.raw["ats_enrichment"] = j
        elif h.endswith("jobs.ashbyhq.com"):
            parts = [p for p in urllib.parse.urlsplit(url).path.split("/") if p]
            if parts:
                board = parts[0]
                obj = client.json(f"https://api.ashbyhq.com/posting-api/job-board/{urllib.parse.quote(board)}?includeCompensation=true")
                best = None
                for j in obj.get("jobs", []):
                    if canonical_url(j.get("applyUrl", "")) == canonical_url(job.apply_url) or canonical_url(j.get("jobUrl", "")) == canonical_url(job.canonical_url):
                        best = j; break
                    if norm(j.get("title")) == norm(job.title): best = j
                if best:
                    job.description = strip_html(best.get("descriptionPlain") or best.get("descriptionHtml")) or job.description
                    job.location_raw = clean_text(best.get("location")) or job.location_raw
                    job.employment_type = clean_text(best.get("employmentType")) or job.employment_type
                    job.remote_status = "remote" if "remote" in norm(best.get("workplaceType") or best.get("location")) else job.remote_status
                    job.canonical_url = canonical_url(best.get("jobUrl") or job.canonical_url); job.apply_url = canonical_url(best.get("applyUrl") or job.apply_url)
                    job.raw["ats_enrichment"] = best
    except Exception as e:
        job.raw["ats_enrichment_error"] = str(e)
    return job


# ---------- Strategy ----------

def pick_profile(job: Job, strategy: dict[str, Any], mode: str) -> tuple[Optional[dict[str, Any]], list[str], float]:
    profiles = [x for x in strategy.get("searches", []) if x.get("enabled", True)]
    allowed = set(strategy.get("strategy", {}).get("run_modes", {}).get(mode, {}).get("profiles", []))
    if allowed:
        profiles = [x for x in profiles if x.get("name") in allowed]
    best: Optional[dict[str, Any]] = None; best_keywords: list[str] = []; best_score = 0.0
    for p in profiles:
        hits: list[tuple[str,float]] = []
        for kw in p.get("keywords", []):
            m = fuzzy_keyword_match(kw, job.title, job.description)
            if m > 0: hits.append((kw,m))
        if not hits: continue
        score = max(m for _,m in hits) + max(0, 3-int(p.get("priority",3))) * 0.03
        if score > best_score:
            best_score = score; best = p; best_keywords = [k for k,m in sorted(hits,key=lambda z:z[1],reverse=True) if m >= 0.68][:6]
    return best, best_keywords, best_score


def remote_gate(job: Job, strategy: dict[str, Any], candidate_state: str) -> tuple[str, str]:
    text = " ".join([job.remote_status, job.location_raw, job.description[:2500]])
    low = norm(text)
    remote_cfg = strategy.get("strategy", {}).get("remote", {})
    reject = [norm(x) for x in remote_cfg.get("reject_markers", [])]
    if any(x and x in low for x in reject): return "reject", "explicit onsite/hybrid requirement"
    if any(norm(x) in low for x in ("must work onsite", "must work on site", "hybrid required", "in person required")):
        return "reject", "explicit onsite/hybrid requirement"
    explicit_remote = job.source_site in {"remotive","jobicy","remoteok","remotelanders"} or job.remote_status.lower()=="remote" or any(norm(x) in low for x in remote_cfg.get("accepted_markers", []))
    # Detect state-limited phrases only when clearly phrased as residence/location restrictions.
    state_patterns = re.findall(r"(?:must (?:live|reside|be located)|residents? of|remote (?:in|from)|eligible states?|based in)\s+([^.;]{2,180})", text, flags=re.I)
    if state_patterns:
        states = set()
        for p in state_patterns: states |= extract_states(p)
        if states and candidate_state.upper() not in states:
            return "reject", f"remote restricted to states excluding {candidate_state.upper()}: {', '.join(sorted(states))}"
    if explicit_remote: return "pass", "explicitly remote / remote-only source"
    return "review", "remote status not explicit enough"


def score_job(job: Job, strategy: dict[str, Any], candidate: dict[str, Any]) -> Job:
    scfg = strategy.get("strategy", {})
    profile, kws, match_strength = pick_profile(job, strategy, getattr(job, "_mode", "fast"))
    if not profile:
        job.recommendation = "OUT_OF_SCOPE"
        return job
    job.search_profile = clean_text(profile.get("name")); job.career_lane = clean_text(profile.get("career_lane")); job.resume_variant = clean_text(profile.get("resume_variant")); job.matched_keywords = kws
    whole = " ".join([job.title, job.description, job.category, " ".join(job.tags)])
    nwhole = norm(whole)
    sig = scfg.get("signals", {})
    job.matched_positive = [x for x in sig.get("strong_positive", []) if norm(x) in nwhole]
    job.matched_accelerators = [x for x in sig.get("career_accelerators", []) if norm(x) in nwhole]
    job.matched_bilingual = [x for x in sig.get("bilingual_bonus", []) if norm(x) in nwhole]
    job.matched_evidence = [x for x in candidate.get("evidence_signals", []) if norm(x) in nwhole]
    job.years_required = years_required(job.description)
    job.remote_gate, job.remote_gate_reason = remote_gate(job, strategy, clean_text(candidate.get("state") or "TX"))
    filters = scfg.get("filters", {})
    reasons: list[str] = []
    tnorm = norm(job.title)
    for term in filters.get("hard_reject_title_terms", []):
        if norm(term) and norm(term) in tnorm: reasons.append(f"title contains hard-exclusion term: {term}")
    for phrase in filters.get("hard_reject_phrases", []):
        if norm(phrase) in nwhole: reasons.append(f"hard-exclusion phrase: {phrase}")
    for cred in filters.get("hard_reject_required_credentials", []):
        if detect_explicit_required_credential(job.description, cred): reasons.append(f"required credential not documented in resume: {cred}")
    if job.remote_gate == "reject": reasons.append(job.remote_gate_reason)
    job.hard_reject_reasons = sorted(set(reasons))

    # Landing score (0-100) mirrors strategy weights.
    direct = min(35.0, 8 + len(set(map(norm,job.matched_evidence))) * 4.2 + match_strength * 9)
    barrier = 20.0
    if job.years_required is not None:
        if job.years_required >= 8: barrier -= 14
        elif job.years_required >= 6: barrier -= 9
        elif job.years_required >= 5: barrier -= 5
        elif job.years_required <= 4: barrier += 0
    soft = filters.get("soft_penalty_phrases", [])
    soft_hits = [x for x in soft if norm(x) in nwhole]
    barrier -= min(7.0, len(soft_hits) * 1.5)
    barrier = max(0.0, min(20.0, barrier))
    priority = int(profile.get("priority", 3)); adjacency = {0:15.0,1:12.0,2:8.5,3:5.0}.get(priority,5.0)
    if "healthcare" in nwhole or any(x in nwhole for x in ("patient","clinical","medical","health plan","provider")): adjacency = min(15, adjacency+1.5)
    age = posted_age_hours(job.posted_at); fresh_cfg = scfg.get("freshness", {})
    if age is None: freshness = float(fresh_cfg.get("unknown_posted_bonus",3))
    elif age < 24: freshness = float(fresh_cfg.get("posted_under_24h_bonus",15))
    elif age <= 72: freshness = float(fresh_cfg.get("posted_24_to_72h_bonus",12))
    elif age <= 168: freshness = float(fresh_cfg.get("posted_3_to_7d_bonus",7))
    elif age <= 336: freshness = float(fresh_cfg.get("posted_8_to_14d_bonus",2))
    else: freshness = float(fresh_cfg.get("posted_over_14d_bonus",0))
    competition = 5.0  # neutral unless a future source provides applicant counts
    friction = 5.0 if job.apply_url and not is_restricted_url(job.apply_url) else (3.0 if job.apply_url else 1.0)
    landing = direct + barrier + adjacency + freshness + competition + friction

    # Career score.
    accel_count = len(set(map(norm,job.matched_accelerators)))
    skill_comp = min(30.0, 8 + accel_count*3.0 + (5 if any(x in nwhole for x in ("process improvement","quality assurance","data quality","implementation","project management")) else 0))
    healthcare = 25.0 if any(x in nwhole for x in ("healthcare","patient","clinical","medical","provider","payer","telehealth","health plan","hipaa")) else (10.0 if job.career_lane=="higher_ed_edtech_hedge" else 4.0)
    path = 0.0
    path += 6 if any(x in nwhole for x in ("quality","audit","compliance")) else 0
    path += 6 if any(x in nwhole for x in ("data","analytics","reporting","dashboard","sql","power bi")) else 0
    path += 6 if any(x in nwhole for x in ("implementation","integration","project","process improvement")) else 0
    path += 2 if any(x in nwhole for x in ("workflow","sop","cross functional","stakeholder")) else 0
    path = min(20.0, path)
    remote_durability = 15.0 if job.remote_gate=="pass" else 7.0
    ann = annualized_salary(job.salary_min, job.salary_max, job.salary_period)
    if ann is None: comp = 5.0
    elif ann >= 90000: comp = 10.0
    elif ann >= 75000: comp = 9.0
    elif ann >= 60000: comp = 7.0
    elif ann >= 50000: comp = 5.0
    elif ann >= 40000: comp = 3.5
    else: comp = 2.0
    career = skill_comp + healthcare + path + remote_durability + comp

    job.landing_score = round(max(0,min(100,landing)),1)
    job.career_score = round(max(0,min(100,career)),1)
    job.door_score = round(0.60*job.landing_score + 0.40*job.career_score,1)
    if job.hard_reject_reasons:
        job.recommendation = "SKIP_HARD_GATE"
    elif job.remote_gate == "review":
        job.recommendation = "REVIEW_REMOTE"
    elif job.career_score >= 80 and job.landing_score >= 55 and job.door_score < 75:
        job.recommendation = "HIGH_VALUE_STRETCH"
    elif job.door_score >= float(scfg.get("scoring",{}).get("apply_now_final_score",75)) and job.career_score >= float(scfg.get("scoring",{}).get("minimum_career_score_for_normal_apply",60)):
        job.recommendation = "APPLY_NOW"
    elif job.door_score >= float(scfg.get("scoring",{}).get("review_final_score",68)):
        job.recommendation = "REVIEW"
    elif job.landing_score >= 72 and job.career_score < 60:
        job.recommendation = "BRIDGE_ONLY"
    else:
        job.recommendation = "LOW_PRIORITY"
    reasons2 = [
        f"profile={job.search_profile}",
        f"landing={job.landing_score:.1f}",
        f"career={job.career_score:.1f}",
        f"freshness={'unknown' if age is None else f'{age/24:.1f}d'}",
    ]
    if job.matched_evidence: reasons2.append("resume evidence: " + ", ".join(job.matched_evidence[:6]))
    if job.matched_accelerators: reasons2.append("career accelerators: " + ", ".join(job.matched_accelerators[:6]))
    if soft_hits: reasons2.append("soft penalties: " + ", ".join(soft_hits[:4]))
    job.score_reasons = reasons2
    return job


# ---------- Browser-assisted universal capture ----------
def browser_capture(config: dict[str, Any], strategy: dict[str, Any], platform: str, url: str = "") -> Job:
    """User-assisted, read-only capture of ONE supplemental job detail page.

    Safety properties: dedicated browser profile, no scripted clicks/fills/submits, downloads disabled,
    local/private network targets blocked, permissions cleared, page must look like a job posting,
    and only bounded text/JobPosting metadata are retained.

    Big-3 traversal is exclusively the v3 normal-Chrome MV3 controller. Keep this legacy
    single-page helper available for supplemental sources, but make accidental Big-3 use
    fail closed before any optional browser automation dependency is imported.
    """
    normalized_platform = clean_text(platform).lower()
    if normalized_platform in {"linkedin", "indeed", "glassdoor"} or is_restricted_url(url):
        raise SystemExit("Big-3 capture is disabled here; use the v3 normal-Chrome extension runner for LinkedIn, Indeed, or Glassdoor.")
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        raise SystemExit("Browser capture requires Playwright. Run the optional browser setup script first.")
    base = Path(config["_base"]); bcfg = config.get("browser", {})
    profile = abs_path(base, bcfg.get("profile_dir", ".browser-profile")); profile.mkdir(parents=True, exist_ok=True)
    require_https = bool(bcfg.get("require_https", True))

    def guard_request(route, request):
        try:
            u=urllib.parse.urlsplit(request.url)
            if u.scheme in {"data","blob","about"}:
                return route.continue_()
            if u.scheme not in {"http","https"} or is_blocked_host((u.hostname or "").lower()):
                return route.abort()
            return route.continue_()
        except Exception:
            return route.abort()

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(profile),
            headless=not bool(bcfg.get("headed", True)),
            accept_downloads=False,
            service_workers="block",
        )
        ctx.clear_permissions()
        ctx.route("**/*", guard_request)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(int(bcfg.get("navigation_timeout_ms",35000)))
        if url:
            try:
                page.goto(validate_web_url(url, require_https=require_https), wait_until="domcontentloaded")
            except Exception as e:
                ctx.close(); raise SystemExit(f"Capture blocked: {e}")
        else:
            homes = {"indeed":"https://www.indeed.com/","linkedin":"https://www.linkedin.com/jobs/","glassdoor":"https://www.glassdoor.com/Job/index.htm"}
            page.goto(homes.get(platform,"https://www.google.com/"), wait_until="domcontentloaded")
            input("Navigate manually to ONE public job-detail page in this dedicated browser, then press ENTER here... ")
        try:
            validate_web_url(page.url, require_https=require_https)
        except Exception as e:
            ctx.close(); raise SystemExit(f"Capture blocked: {e}")
        data = page.evaluate("""() => {
          const scripts=[...document.querySelectorAll('script[type="application/ld+json"]')].slice(0,40).map(x=>(x.textContent||'').slice(0,80000));
          const text=(sel)=>{const e=document.querySelector(sel); return e ? (e.innerText||e.textContent||'').trim() : ''};
          return {url:location.href,title:(document.title||'').slice(0,1000),h1:text('h1').slice(0,2000),body:(document.body?.innerText||'').slice(0,120000),scripts};
        }""")
        ctx.close()
    jp = None
    for raw in data.get("scripts",[]):
        try:
            obj=json.loads(raw)
        except Exception: continue
        stack = obj if isinstance(obj,list) else [obj]
        for item in stack:
            if isinstance(item,dict) and item.get("@graph"):
                stack += [x for x in item.get("@graph",[]) if isinstance(x,dict)]
            if isinstance(item,dict) and (item.get("@type")=="JobPosting" or "JobPosting" in (item.get("@type") if isinstance(item.get("@type"),list) else [])):
                jp=item; break
        if jp: break
    page_text=norm(" ".join([clean_text(data.get("title")),clean_text(data.get("h1")),clean_text(data.get("body"))[:30000]]))
    job_markers=("job description","responsibilities","qualifications","requirements","about the role","employment type","salary","compensation","apply now","position summary")
    if not jp and sum(1 for m in job_markers if m in page_text) < 2:
        raise SystemExit("Capture refused: this page does not confidently look like a job-detail page. Navigate to the actual posting and try again.")
    safe_page_meta={"url":safe_output_url(data.get("url","")),"title":clean_text(data.get("title"))[:1000],"h1":clean_text(data.get("h1"))[:2000]}
    if jp:
        org=jp.get("hiringOrganization") or {}; loc=jp.get("jobLocation") or {}; locs=loc if isinstance(loc,list) else [loc]
        loc_parts=[]
        for l in locs:
            if not isinstance(l,dict): continue
            a=l.get("address") or {}; loc_parts.append(", ".join(clean_text(a.get(k)) for k in ("addressLocality","addressRegion","addressCountry") if clean_text(a.get(k))))
        bs=jp.get("baseSalary") or {}; val=bs.get("value") or {}; mn=val.get("minValue"); mx=val.get("maxValue"); per=clean_text(val.get("unitText") or "year")
        saltxt = f"{mn or ''} - {mx or ''} {clean_text(bs.get('currency') or '')} {per}".strip(" -")
        apply = safe_output_url(clean_text(jp.get("url") or data.get("url","")))
        j=Job(source_site=platform or host_of(data.get("url","")), source_job_id=clean_text(jp.get("identifier") if not isinstance(jp.get("identifier"),dict) else (jp.get("identifier") or {}).get("value")), canonical_url=canonical_url(safe_output_url(data.get("url",""))), apply_url=canonical_url(apply), title=clean_text(jp.get("title") or data.get("h1")), company=clean_text(org.get("name")), location_raw=clean_text(" | ".join(x for x in loc_parts if x) or jp.get("applicantLocationRequirements") or ""), remote_status="remote" if "telecommute" in norm(jp.get("jobLocationType")) or "remote" in norm(jp.get("jobLocationType")) else "unknown", employment_type=clean_text(jp.get("employmentType")), salary_text=saltxt, salary_min=float(mn) if isinstance(mn,(int,float)) else None, salary_max=float(mx) if isinstance(mx,(int,float)) else None, salary_currency=clean_text(bs.get("currency") or "USD"), salary_period=per, posted_at=clean_text(jp.get("datePosted")), description=strip_html(jp.get("description"))[:120000], raw={"jsonld":jp,"page":safe_page_meta})
    else:
        u=safe_output_url(data.get("url",""))
        j=Job(source_site=platform or host_of(u), canonical_url=canonical_url(u), apply_url=canonical_url(u), title=clean_text(data.get("h1") or data.get("title")), company="", location_raw="", remote_status="unknown", description=clean_text(data.get("body"))[:120000], raw={"page":safe_page_meta,"capture":"unstructured-job-page"})
    setattr(j,"_mode","deep")
    return score_job(j,strategy,config.get("candidate",{}))


# ---------- Manual search links ----------
def search_url(platform: str, keyword: str) -> str:
    q=urllib.parse.quote_plus(keyword)
    if platform=="indeed": return f"https://www.indeed.com/jobs?q={q}&l=Remote"
    if platform=="linkedin": return f"https://www.linkedin.com/jobs/search/?keywords={q}&location=United%20States&f_WT=2&f_TPR=r604800"
    if platform=="glassdoor": return f"https://www.google.com/search?q=site%3Aglassdoor.com%2FJob+%22{q}%22+remote"
    if platform=="ziprecruiter": return f"https://www.ziprecruiter.com/jobs-search?search={q}&location=Remote"
    if platform=="builtin": return f"https://builtin.com/jobs/remote?search={q}"
    if platform=="flexjobs": return f"https://www.google.com/search?q=site%3Aflexjobs.com+%22{q}%22+remote"
    if platform=="higheredjobs": return f"https://www.google.com/search?q=site%3Ahigheredjobs.com+%22{q}%22+remote"
    if platform=="herc": return f"https://www.google.com/search?q=site%3Ahercjobs.org+%22{q}%22+remote"
    if platform=="insidehighered": return f"https://www.google.com/search?q=site%3Acareers.insidehighered.com+%22{q}%22+remote"
    return f"https://www.google.com/search?q={q}+remote+jobs"


def build_manual_links(strategy: dict[str,Any], config: dict[str,Any], out: Path, mode: str) -> None:
    allowed=set(strategy.get("strategy",{}).get("run_modes",{}).get(mode,{}).get("profiles",[]))
    profiles=[p for p in strategy.get("searches",[]) if p.get("enabled",True) and (not allowed or p.get("name") in allowed)]
    enabled=[k for k,v in config.get("manual_platforms",{}).items() if v]
    rows=[]
    for p in sorted(profiles,key=lambda x:int(x.get("priority",3))):
        for kw in p.get("keywords",[]):
            for plat in enabled:
                # Route higher-ed specialist boards only to P2.
                if plat in {"higheredjobs","herc","insidehighered"} and int(p.get("priority",3)) != 2: continue
                if plat in {"builtin"} and int(p.get("priority",3)) > 1: continue
                rows.append((int(p.get("priority",3)),p.get("name",""),kw,plat,search_url(plat,kw)))
    body=["""<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; connect-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"><meta name="referrer" content="no-referrer"><title>Manual Search Links</title><style>body{font-family:system-ui;margin:24px;max-width:1200px}table{border-collapse:collapse;width:100%}th,td{padding:8px;border-bottom:1px solid #ddd;text-align:left}input{padding:10px;width:100%;max-width:500px}.p0{background:#eefbf2}.p1{background:#eef5ff}.p2{background:#faf5ff}.p3{background:#fff7ed}</style>""","<h1>Remote job search links</h1><p>These links are for normal user browsing on major boards. They are not autonomously crawled. Use the site's Remote filter and verify it remains selected.</p><input id='q' placeholder='filter links...'><table id='t'><thead><tr><th>P</th><th>Profile</th><th>Keyword</th><th>Platform</th><th>Open</th></tr></thead><tbody>"]
    for pr,pn,kw,plat,url in rows:
        body.append(f"<tr class='p{pr}'><td>P{pr}</td><td>{html.escape(pn)}</td><td>{html.escape(kw)}</td><td>{html.escape(plat)}</td><td><a target='_blank' rel='noopener noreferrer' referrerpolicy='no-referrer' href='{html.escape(url,quote=True)}'>search</a></td></tr>")
    body.append("</tbody></table><script>const q=document.getElementById('q');q.oninput=()=>{for(const r of document.querySelectorAll('#t tbody tr'))r.style.display=r.innerText.toLowerCase().includes(q.value.toLowerCase())?'':'none'}</script>")
    (out/"manual_search_links.html").write_text("".join(body),encoding="utf-8")


# ---------- Exports ----------
def parse_json_cell(v: str) -> list[str]:
    try:
        x=json.loads(v or "[]"); return x if isinstance(x,list) else []
    except Exception: return []


def export_all(store: Store, out: Path, strategy: dict[str,Any], config: dict[str,Any], mode: str) -> None:
    out.mkdir(parents=True, exist_ok=True); rows=store.rows()
    fields=["job_id","recommendation","door_score","landing_score","career_score","title","company","location_raw","salary_text","posted_at","career_lane","search_profile","resume_variant","remote_gate","employment_type","canonical_url","apply_url","application_status","notes"]
    for name, filt in [("all_jobs",lambda r:True),("apply_now",lambda r:r["recommendation"]=="APPLY_NOW"),("review",lambda r:r["recommendation"] in {"REVIEW","REVIEW_REMOTE"}),("stretch",lambda r:r["recommendation"]=="HIGH_VALUE_STRETCH")]:
        with (out/f"{name}.csv").open("w",newline="",encoding="utf-8-sig") as f:
            w=csv.DictWriter(f,fieldnames=fields); w.writeheader()
            for r in rows:
                if filt(r): w.writerow({k:csv_safe_cell(r[k]) for k in fields})
    with (out/"jobs.jsonl").open("w",encoding="utf-8") as f:
        for r in rows: f.write(json.dumps(dict(r),ensure_ascii=False)+"\n")
    # HTML dashboard
    data=[]
    for r in rows:
        d={k:r[k] for k in fields}; d["score_reasons"]=parse_json_cell(r["score_reasons_json"]); d["hard_reject_reasons"]=parse_json_cell(r["hard_reject_reasons_json"]); d["matched_keywords"]=parse_json_cell(r["matched_keywords_json"]); d["description"]=r["description"] or ""; data.append(d)
    js=json.dumps(data,ensure_ascii=False).replace("</","<\\/")
    dashboard=f"""<!doctype html><meta charset='utf-8'><meta http-equiv='Content-Security-Policy' content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; connect-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"><meta name='referrer' content='no-referrer'><title>Remote Career Job Queue</title>
<style>body{{font-family:Inter,system-ui,sans-serif;margin:0;background:#f6f8fb;color:#172033}}header{{padding:22px 28px;background:white;border-bottom:1px solid #e6e9ef;position:sticky;top:0;z-index:5}}h1{{margin:0 0 4px}}.controls{{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}}input,select{{font:inherit;padding:9px 11px;border:1px solid #ccd3df;border-radius:9px;background:white}}main{{padding:20px 28px}}.summary{{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px}}.chip{{background:white;border:1px solid #e3e7ef;border-radius:12px;padding:10px 13px}}.job{{background:white;border:1px solid #e2e7ef;border-radius:14px;padding:16px;margin:10px 0}}.top{{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}}.scores{{white-space:nowrap;font-weight:700}}.meta{{color:#5a6678;margin:6px 0}}.tag{{display:inline-block;padding:4px 8px;border-radius:999px;background:#eef2f8;margin:2px;font-size:12px}}.APPLY_NOW{{border-left:6px solid #15965a}}.HIGH_VALUE_STRETCH{{border-left:6px solid #6f55d8}}.REVIEW,.REVIEW_REMOTE{{border-left:6px solid #377bd8}}.SKIP_HARD_GATE{{opacity:.62}}details{{margin-top:8px}}a{{color:#1359b2}}</style>
<header><h1>Remote Career Job Queue</h1><div>Landing speed + long-term career value</div><div class='controls'><input id='q' placeholder='Search title/company/description'><select id='rec'><option value=''>All recommendations</option><option>APPLY_NOW</option><option>HIGH_VALUE_STRETCH</option><option>REVIEW</option><option>REVIEW_REMOTE</option><option>BRIDGE_ONLY</option><option>LOW_PRIORITY</option><option>SKIP_HARD_GATE</option></select><select id='lane'><option value=''>All lanes</option></select></div></header><main><div class='summary' id='sum'></div><div id='list'></div></main>
<script>const data={js};const q=document.getElementById('q'),rec=document.getElementById('rec'),lane=document.getElementById('lane'),list=document.getElementById('list'),sum=document.getElementById('sum');[...new Set(data.map(x=>x.career_lane).filter(Boolean))].sort().forEach(x=>lane.add(new Option(x,x)));function esc(s){{return String(s??'').replace(/[&<>\"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}}[c]))}}function safeHref(s){{try{{const u=new URL(String(s||''));return ['http:','https:'].includes(u.protocol)?u.href:'#'}}catch(e){{return '#'}}}}function render(){{let v=data.filter(x=>(!rec.value||x.recommendation===rec.value)&&(!lane.value||x.career_lane===lane.value)&&(!q.value||(x.title+' '+x.company+' '+x.description).toLowerCase().includes(q.value.toLowerCase())));const counts={{}};v.forEach(x=>counts[x.recommendation]=(counts[x.recommendation]||0)+1);sum.innerHTML=`<div class='chip'><b>${{v.length}}</b> shown</div>`+Object.entries(counts).map(([k,n])=>`<div class='chip'><b>${{n}}</b> ${{esc(k)}}</div>`).join('');list.innerHTML=v.slice(0,500).map(x=>`<section class='job ${{esc(x.recommendation)}}'><div class='top'><div><b>${{esc(x.title)}}</b><div>${{esc(x.company)}}</div></div><div class='scores'>Door ${{x.door_score}} · Land ${{x.landing_score}} · Career ${{x.career_score}}</div></div><div class='meta'>${{esc(x.location_raw)}} · ${{esc(x.salary_text||'salary unknown')}} · ${{esc(x.posted_at||'date unknown')}}</div><span class='tag'>${{esc(x.recommendation)}}</span><span class='tag'>${{esc(x.career_lane)}}</span><span class='tag'>${{esc(x.resume_variant)}}</span><div><a target='_blank' rel='noopener noreferrer' referrerpolicy='no-referrer' href='${{esc(safeHref(x.apply_url||x.canonical_url))}}'>Open / apply</a> · <code>${{esc(x.job_id)}}</code></div><details><summary>Why / description</summary><p>${{esc((x.score_reasons||[]).join(' · '))}}</p>${{x.hard_reject_reasons?.length?`<p><b>Hard gate:</b> ${{esc(x.hard_reject_reasons.join('; '))}}</p>`:''}}<p>${{esc((x.description||'').slice(0,6000))}}</p></details></section>`).join('')}}q.oninput=rec.onchange=lane.onchange=render;render();</script>"""
    (out/"jobs.html").write_text(dashboard,encoding="utf-8")
    build_manual_links(strategy,config,out,mode)
    # Decision batches for ChatGPT
    candidates=[r for r in rows if r["recommendation"] in {"APPLY_NOW","HIGH_VALUE_STRETCH","REVIEW"}]
    batch_dir=out/"chatgpt_batches"; batch_dir.mkdir(exist_ok=True)
    for old in batch_dir.glob("*.md"): old.unlink()
    for bi in range(0,len(candidates),10):
        b=candidates[bi:bi+10]; parts=["# Job Decision Batch\n\nGoal: choose the jobs most likely to produce a good remote offer quickly while preserving the regulated-healthcare → data/quality → analytics/implementation career path.\n"]
        for r in b:
            parts.append(f"\n## {r['job_id']} — {r['title']} — {r['company']}\nRecommendation: {r['recommendation']} | Door {r['door_score']} | Landing {r['landing_score']} | Career {r['career_score']}\nLane: {r['career_lane']} | Remote: {r['remote_gate']} | Salary: {r['salary_text'] or 'unknown'} | Posted: {r['posted_at'] or 'unknown'}\nURL: {r['apply_url'] or r['canonical_url']}\nMatched search: {', '.join(parse_json_cell(r['matched_keywords_json']))}\nReasons: {'; '.join(parse_json_cell(r['score_reasons_json']))}\n\nDescription:\n{(r['description'] or '')[:8000]}\n")
        (batch_dir/f"batch_{bi//10+1:03d}.md").write_text("\n".join(parts),encoding="utf-8")
    # Market report
    total=len(rows); recs={}; lanes={}; sources={}
    for r in rows:
        recs[r["recommendation"]]=recs.get(r["recommendation"],0)+1; lanes[r["career_lane"]]=lanes.get(r["career_lane"],0)+1
        for o in store.source_occurrences(r["job_id"]): sources[o["source_site"]]=sources.get(o["source_site"],0)+1
    report=["# Search Run Market Report",f"\nGenerated: {now_iso()}",f"\nTotal unique jobs in database: **{total}**","\n## Recommendation counts"]
    report += [f"- {k}: {v}" for k,v in sorted(recs.items(),key=lambda x:-x[1])]
    report += ["\n## Career-lane counts"]+[f"- {k or 'unclassified'}: {v}" for k,v in sorted(lanes.items(),key=lambda x:-x[1])]
    report += ["\n## Source occurrences"]+[f"- {k}: {v}" for k,v in sorted(sources.items(),key=lambda x:-x[1])]
    report += ["\n## Top jobs"]
    for r in candidates[:25]: report.append(f"- **{r['door_score']:.1f}** {r['recommendation']} — {r['title']} — {r['company']} — {r['location_raw']} — {r['apply_url'] or r['canonical_url']}")
    (out/"market_report.md").write_text("\n".join(report)+"\n",encoding="utf-8")


def print_summary(store: Store) -> None:
    rows=store.rows(); rec={}
    for r in rows: rec[r["recommendation"]]=rec.get(r["recommendation"],0)+1
    print(f"Unique jobs in database: {len(rows)}")
    for k,v in sorted(rec.items(),key=lambda x:-x[1]): print(f"  {k:20s} {v}")
    print("\nTop candidates:")
    for r in rows[:15]:
        if r["recommendation"]=="OUT_OF_SCOPE": continue
        print(f"  {r['door_score']:5.1f}  {r['recommendation']:<18} {r['title']} — {r['company']}")


def run_search(config: dict[str,Any], strategy: dict[str,Any], mode: str) -> int:
    base=Path(config["_base"]); acfg=config.get("app",{}); out=abs_path(base,acfg.get("output_dir","out")); db=abs_path(base,acfg.get("db_path","data/jobs.sqlite3")); cache=abs_path(base,acfg.get("cache_dir","cache"))
    client=HttpClient(cache,int(acfg.get("cache_minutes",60)),int(acfg.get("http_timeout_seconds",25)),clean_text(acfg.get("user_agent")) or "RemoteCareerJobSearch/1.0")
    store=Store(db); source_status={}; jobs: list[Job]=[]
    fetchers=[("remotive",fetch_remotive),("jobicy",fetch_jobicy),("remoteok",fetch_remoteok),("remotelanders",fetch_remotelanders)]
    print(f"Remote Career Job Search v{VERSION} — mode={mode}")
    for name,fn in fetchers:
        scfg=config.get("sources",{}).get(name,{})
        if not scfg.get("enabled",False): continue
        try:
            print(f"[{name}] retrieving...")
            got=fn(client,scfg); limit=int(acfg.get("max_jobs_per_source",1200)); got=got[:limit]; jobs.extend(got); source_status[name]={"ok":True,"count":len(got)}; print(f"[{name}] {len(got)} rows")
        except Exception as e:
            source_status[name]={"ok":False,"error":str(e)}; print(f"[{name}] ERROR: {e}",file=sys.stderr)
    try:
        ats=fetch_direct_ats_watch(client,config); jobs.extend(ats)
        if ats: source_status["ats_watch"]={"ok":True,"count":len(ats)}
    except Exception as e:
        source_status["ats_watch"]={"ok":False,"error":str(e)}
    # First-pass profile match determines which direct ATS jobs are worth enriching.
    for j in jobs: setattr(j,"_mode",mode)
    enrich_cfg=config.get("enrichment",{}); enrich_left=int(enrich_cfg.get("max_jobs_per_run",100))
    if enrich_cfg.get("public_ats",True):
        for j in jobs:
            p,_,score=pick_profile(j,strategy,mode)
            if p and enrich_left>0 and j.apply_url and not j.description and any(x in host_of(j.apply_url) for x in ("lever.co","greenhouse.io","ashbyhq.com")):
                enrich_public_ats(client,j); enrich_left-=1
    new=0; updated=0; matched=0
    for j in jobs:
        score_job(j,strategy,config.get("candidate",{}))
        if j.recommendation=="OUT_OF_SCOPE": continue
        matched+=1
        if store.upsert(j): new+=1
        else: updated+=1
    export_all(store,out,strategy,config,mode)
    print(f"\nMatched strategy: {matched} | new unique jobs: {new} | updated: {updated}")
    print_summary(store)
    print(f"\nOpen dashboard: {out/'jobs.html'}")
    print(f"Apply queue:    {out/'apply_now.csv'}")
    print(f"ChatGPT batches:{out/'chatgpt_batches'}")
    print(f"Manual links:   {out/'manual_search_links.html'}")
    store.close(); return 0


def self_test(config: dict[str,Any], strategy: dict[str,Any]) -> int:
    samples=[
        Job(source_site="fixture",title="Patient Enrollment Specialist",company="Digital Health Co",location_raw="Remote - United States",remote_status="remote",employment_type="Full-time",salary_text="$55,000 - $65,000",salary_min=55000,salary_max=65000,posted_at=now_iso(),description="Manage remote patient monitoring enrollment, patient onboarding, HIPAA documentation, workflow handoffs, Excel reporting, quality assurance and process improvement."),
        Job(source_site="fixture",title="Healthcare Quality Implementations Specialist",company="Health Plan Co",location_raw="Remote - United States",remote_status="remote",employment_type="Full-time",salary_text="$75,000 - $95,000",salary_min=75000,salary_max=95000,posted_at=now_iso(),description="Support healthcare quality implementations, data validation, quality improvement, cross-functional project coordination, dashboards and Power BI. 3 years of experience required."),
        Job(source_site="fixture",title="Healthcare Quality Specialist",company="Health Plan Co",location_raw="Remote",remote_status="remote",employment_type="Full-time",posted_at=now_iso(),description="Healthcare quality improvement and documentation audits. Active RN license required."),
        Job(source_site="fixture",title="Patient Access Specialist",company="Hospital Co",location_raw="Hybrid Austin, TX",remote_status="hybrid",posted_at=now_iso(),description="Hybrid schedule required three days onsite."),
    ]
    for j in samples:
        setattr(j,"_mode","deep"); score_job(j,strategy,config.get("candidate",{})); print(f"{j.title}: {j.recommendation} Door={j.door_score} Landing={j.landing_score} Career={j.career_score} gates={j.hard_reject_reasons}")
    assert samples[0].recommendation in {"APPLY_NOW","REVIEW","HIGH_VALUE_STRETCH"}
    assert samples[2].recommendation=="SKIP_HARD_GATE"
    assert samples[3].recommendation=="SKIP_HARD_GATE"
    assert validate_web_url("https://example.com/jobs/1",require_https=True).startswith("https://")
    for bad in ("file:///etc/passwd","javascript:alert(1)","http://localhost:8000/x","http://127.0.0.1/x","http://10.0.0.5/x","https://user:pass@example.com/x"):
        try:
            validate_web_url(bad)
            raise AssertionError(f"unsafe URL was not blocked: {bad}")
        except ValueError:
            pass
    assert csv_safe_cell("=HYPERLINK(\"https://evil.invalid\")").startswith("'")
    print("SELF-TEST PASSED")
    return 0




def security_check(config: dict[str,Any]) -> int:
    bcfg=config.get("browser",{})
    tests=[]
    def t(name, fn):
        try:
            fn(); tests.append((name,True,""))
        except Exception as e:
            tests.append((name,False,str(e)))
    t("Public HTTPS URL accepted", lambda: validate_web_url("https://example.com/jobs/123",require_https=True))
    for name,u in [
        ("file:// blocked","file:///etc/passwd"),
        ("javascript: blocked","javascript:alert(1)"),
        ("localhost blocked","http://localhost:8000/x"),
        ("loopback blocked","http://127.0.0.1/x"),
        ("private IPv4 blocked","http://10.0.0.5/x"),
        ("embedded credentials blocked","https://user:pass@example.com/x"),
    ]:
        def should_block(u=u):
            try: validate_web_url(u); raise RuntimeError("unsafe URL was accepted")
            except ValueError: return True
        t(name,should_block)
    t("Dangerous URL canonicalizes to empty", lambda: (_ for _ in ()).throw(RuntimeError("not blocked")) if canonical_url("javascript:alert(1)") else True)
    t("CSV formula neutralized", lambda: (_ for _ in ()).throw(RuntimeError("not neutralized")) if not str(csv_safe_cell("=1+1")).startswith("'") else True)
    print(f"Security check — Remote Career Job Search v{VERSION}")
    print(f"  Browser HTTPS-only: {bool(bcfg.get('require_https',True))}")
    print("  Automated form filling/submission: disabled")
    print("  CAPTCHA/access-control bypass: disabled")
    print("  Browser downloads: disabled")
    print("  Browser service workers: blocked during capture")
    print("  Browser permissions: cleared during capture")
    print("  Local/private-network capture: blocked")
    print("  Unknown/non-job page capture: refused")
    print("  Local dashboard outbound network access: blocked by CSP")
    print("  CSV formula injection protection: enabled")
    bad=0
    for name,ok,msg in tests:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(': '+msg) if msg else ''}")
        bad += 0 if ok else 1
    if bad:
        print(f"SECURITY CHECK FAILED: {bad} test(s)")
        return 1
    print("SECURITY CHECK PASSED")
    return 0


def main() -> int:
    ap=argparse.ArgumentParser(description="Remote career job-search automation")
    ap.add_argument("--config",default="config.toml")
    sub=ap.add_subparsers(dest="cmd",required=True)
    p=sub.add_parser("run",help="Retrieve, dedupe, score, and export jobs"); p.add_argument("--mode",choices=["fast","deep"],default="fast")
    sub.add_parser("stats",help="Show current database summary")
    p=sub.add_parser("capture",help="Capture one job detail page in a persistent browser"); p.add_argument("--platform",default="web"); p.add_argument("--url",default="")
    p=sub.add_parser("mark",help="Mark application status"); p.add_argument("job_id"); p.add_argument("status"); p.add_argument("--notes",default="")
    sub.add_parser("open",help="Open the local job dashboard")
    sub.add_parser("open-searches",help="Open the generated major-board search links")
    sub.add_parser("self-test",help="Run offline scoring/gating tests")
    sub.add_parser("security-check",help="Verify local/browser safety guardrails")
    args=ap.parse_args()
    config_path=Path(args.config).resolve(); base=config_path.parent; config=load_toml(config_path); config["_base"]=str(base)
    strat_path=abs_path(base,config.get("app",{}).get("strategy_file","strategy.toml")); strategy=load_toml(strat_path)
    acfg=config.get("app",{}); db=abs_path(base,acfg.get("db_path","data/jobs.sqlite3")); out=abs_path(base,acfg.get("output_dir","out"))
    if args.cmd=="run": return run_search(config,strategy,args.mode)
    if args.cmd=="self-test": return self_test(config,strategy)
    if args.cmd=="security-check": return security_check(config)
    if args.cmd=="stats":
        s=Store(db); print_summary(s); s.close(); return 0
    if args.cmd=="mark":
        s=Store(db); s.mark(args.job_id,args.status,args.notes); s.close(); print(f"Marked {args.job_id} -> {args.status}"); return 0
    if args.cmd=="open":
        target=out/"jobs.html"
        if not target.exists(): print("Run a search first: python jobbot.py run --mode fast"); return 1
        webbrowser.open(target.resolve().as_uri()); return 0
    if args.cmd=="open-searches":
        target=out/"manual_search_links.html"
        if not target.exists(): build_manual_links(strategy,config,out,"deep")
        webbrowser.open(target.resolve().as_uri()); return 0
    if args.cmd=="capture":
        j=browser_capture(config,strategy,args.platform,args.url)
        s=Store(db); s.upsert(j); export_all(s,out,strategy,config,"deep"); s.close()
        print(f"Captured {j.job_id}: {j.title} — {j.company} | {j.recommendation} Door={j.door_score}")
        return 0
    return 2


if __name__=="__main__":
    raise SystemExit(main())
