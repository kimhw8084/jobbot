from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from .config import ConfigBundle
from .db import Database
from .legacy_engine import Job, PrecisionStore


@contextmanager
def open_ledger(bundle: ConfigBundle) -> Iterator[PrecisionStore]:
    Database(bundle).migrate()
    store = PrecisionStore(bundle.database_path)
    try:
        yield store
    finally:
        store.close()


__all__ = ["Job", "PrecisionStore", "open_ledger"]
