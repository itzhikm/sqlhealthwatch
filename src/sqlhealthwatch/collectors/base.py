"""The collector contract.

A collector is self-contained: it owns the T-SQL it runs, the repository table it writes, the
transform between them, and (through ``analyze/thresholds.py``) the thresholds it feeds. Adding a
metric is one collector module, one table, and a threshold entry -- no other module changes.

Version handling lives in two places only: ``applies_to`` decides whether the collector runs at all
on this instance, and ``sql_file_for`` picks between sibling SQL variants. Neither branches inside
a query.
"""

from __future__ import annotations

import functools
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import pandas as pd

from ..config import ServerConfig, Settings
from ..connection import SqlConnection
from ..version import DatabaseInfo, ServerFeatures

log = logging.getLogger(__name__)

Tier = Literal["fast", "daily"]


@dataclass
class ServerContext:
    """Everything a collector needs about the instance it is running against."""

    server: ServerConfig
    features: ServerFeatures
    connection: SqlConnection
    settings: Settings
    sql_dir: Path
    run_id: str
    server_id: int
    collected_at_utc: datetime
    tier: Tier = "fast"
    # Filled by the runner from mon.collector_watermark, for incremental collectors.
    watermarks: dict[str, datetime | None] = field(default_factory=dict)
    # Notes a collector wants surfaced on the report page (degraded paths, skipped work).
    notes: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.server.name

    def target_databases(self) -> list[DatabaseInfo]:
        """User databases the per-database collectors should visit.

        Excludes anything in ``collection.exclude_databases``, read-only secondaries' snapshots
        (already filtered by databases.sql), and -- unless configured otherwise -- system databases.
        """
        excluded = {name.lower() for name in self.settings.collection.exclude_databases}
        system = {"master", "model", "msdb", "tempdb"}
        result = []
        for db in self.features.databases:
            lowered = db.name.lower()
            if lowered in excluded:
                continue
            if lowered in system and not self.settings.collection.include_system_databases:
                continue
            result.append(db)
        return result

    def note(self, message: str) -> None:
        log.debug("%s: %s", self.name, message)
        self.notes.append(message)


class Collector(ABC):
    """Base class for every collector."""

    name: str = ""
    tier: Tier = "fast"
    table: str = ""
    sql_file: str | None = None

    # ------------------------------------------------------------------------------ gating

    def applies_to(self, ctx: ServerContext) -> bool:
        """Whether this collector can run on this instance at all.

        Returning False is a normal outcome on a mixed fleet, not an error -- the run continues and
        the report notes what was skipped.
        """
        return True

    def sql_file_for(self, ctx: ServerContext) -> str | None:
        """Which SQL variant to use. Override where a legacy sibling file exists."""
        return self.sql_file

    # ------------------------------------------------------------------------------ running

    def params_for(self, ctx: ServerContext) -> list[Any]:
        return []

    def fetch(self, ctx: ServerContext) -> list[dict]:
        sql_file = self.sql_file_for(ctx)
        if not sql_file:
            return []
        sql = load_sql(ctx.sql_dir, sql_file)
        return ctx.connection.query(sql, self.params_for(ctx))

    @abstractmethod
    def transform(self, rows: list[dict], ctx: ServerContext) -> pd.DataFrame:
        """Normalize raw rows into the shape of :attr:`table`.

        Kept free of database access so it can be tested against captured JSON fixtures.
        """

    def collect(self, ctx: ServerContext) -> pd.DataFrame:
        """Fetch, transform and stamp identity columns onto every row."""
        frame = self.transform(self.fetch(ctx), ctx)
        return stamp(frame, ctx)


class PerDatabaseCollector(Collector):
    """A collector whose query runs once per database rather than once per instance.

    A failure in one database (offline, permissions, a snapshot going away mid-run) is logged and
    skipped -- one database never costs the whole server its collection.
    """

    def databases(self, ctx: ServerContext) -> list[DatabaseInfo]:
        return ctx.target_databases()

    def fetch(self, ctx: ServerContext) -> list[dict]:
        sql_file = self.sql_file_for(ctx)
        if not sql_file:
            return []
        sql = load_sql(ctx.sql_dir, sql_file)
        rows: list[dict] = []
        for db in self.databases(ctx):
            try:
                rows.extend(ctx.connection.query_in_database(db.name, sql, self.params_for(ctx)))
            except Exception as exc:
                log.warning("%s/%s: %s skipped (%s)", ctx.name, db.name, self.name, exc)
                ctx.note(f"{self.name}: skipped database {db.name} ({_brief(exc)})")
        return rows


@functools.lru_cache(maxsize=128)
def _read_sql(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def load_sql(sql_dir: Path, filename: str) -> str:
    """Read a query from ``sql/``.

    Queries live as files rather than inline strings so a DBA can read, diff and tune them without
    touching Python, and so version variants sit side by side.
    """
    path = sql_dir / filename
    if not path.exists():
        raise FileNotFoundError(f"missing SQL file: {path}")
    return _read_sql(str(path))


def stamp(frame: pd.DataFrame, ctx: ServerContext) -> pd.DataFrame:
    """Tag every row with run/server/time identity, in a stable column order."""
    if frame is None or frame.empty:
        return pd.DataFrame()
    frame = frame.copy()
    frame.insert(0, "collected_at_utc", ctx.collected_at_utc)
    frame.insert(0, "server_id", ctx.server_id)
    frame.insert(0, "run_id", ctx.run_id)
    return frame


def _brief(exc: Exception) -> str:
    return str(exc).strip().replace("\n", " ")[:200]
