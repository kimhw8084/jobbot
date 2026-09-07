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
)


def all_migrations() -> list[ModuleType]:
    return [import_module(name) for name in MIGRATION_MODULES]
