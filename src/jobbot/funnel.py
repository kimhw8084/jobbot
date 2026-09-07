from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class FunnelRow:
    dimension: str
    value: str
    applications: int
    successes: int
    raw_rate: float
    adjusted_rate: float
    stable: bool


def analyze(conn: sqlite3.Connection, *, prior_strength: float = 10.0, minimum_family_sample: int = 12) -> list[FunnelRow]:
    overall = conn.execute("SELECT COUNT(*) FROM jobs WHERE upper(application_status) IN ('APPLIED','SCREEN','INTERVIEW','FINAL','OFFER','REJECTED')").fetchone()[0]
    overall_success = conn.execute("SELECT COUNT(*) FROM jobs WHERE upper(application_status) IN ('SCREEN','INTERVIEW','FINAL','OFFER')").fetchone()[0]
    overall_rate = (overall_success / overall) if overall else 0.0
    dimensions = {
        "lane": "COALESCE(NULLIF(career_lane,''),'unknown')",
        "title_family": "COALESCE(NULLIF(normalized_title_family,''),title,'unknown')",
        "resume": "COALESCE(NULLIF(resume_variant,''),'unknown')",
        "door_bucket": "CAST(CAST(COALESCE(door_score,0)/10 AS INTEGER)*10 AS TEXT)||'-'||CAST(CAST(COALESCE(door_score,0)/10 AS INTEGER)*10+9 AS TEXT)",
    }
    output: list[FunnelRow] = []
    for dimension, expression in dimensions.items():
        query = f"""SELECT {expression} value,
          SUM(CASE WHEN upper(application_status) IN ('APPLIED','SCREEN','INTERVIEW','FINAL','OFFER','REJECTED') THEN 1 ELSE 0 END) applications,
          SUM(CASE WHEN upper(application_status) IN ('SCREEN','INTERVIEW','FINAL','OFFER') THEN 1 ELSE 0 END) successes
          FROM jobs GROUP BY value HAVING applications>0 ORDER BY applications DESC"""
        for value, applications, successes in conn.execute(query):
            apps, wins = int(applications), int(successes)
            raw = wins / apps if apps else 0.0
            adjusted = (wins + prior_strength * overall_rate) / (apps + prior_strength)
            output.append(FunnelRow(dimension, str(value), apps, wins, raw, adjusted, apps >= minimum_family_sample))
    return output
