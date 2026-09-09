from __future__ import annotations

from importlib import import_module
from types import ModuleType

MIGRATION_MODULES = (
    "jobbot.migrations.m0001_ledger",
    "jobbot.migrations.m0002_browser_tasks",
    "jobbot.migrations.m0003_product",
    "jobbot.migrations.m0004_source_occurrences",
    "jobbot.migrations.m0005_structured_evidence",
    "jobbot.migrations.m0006_diff_backfill",
    "jobbot.migrations.m0007_durable_discoveries",
    "jobbot.migrations.m0008_page_reconciliation",
    "jobbot.migrations.m0009_task_phases",
    "jobbot.migrations.m0010_description_state",
    "jobbot.migrations.m0011_watch_state",
    "jobbot.migrations.m0012_watch_stop_latch",
    "jobbot.migrations.m0013_platform_state",
)


def all_migrations() -> list[ModuleType]:
    return [import_module(name) for name in MIGRATION_MODULES]
