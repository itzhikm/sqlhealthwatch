"""Stale statistics.

Two paths, chosen by probe rather than by version number, because this is service-pack gated:

    sys.dm_db_stats_properties (2008 R2 SP2 / 2012 SP1+)
        last_updated and modification_counter are both exact and per-statistic.

    STATS_DATE() + sys.sysindexes.rowmodctr (older)
        last_updated is still exact; the modification counter is not. rowmodctr is per *table*, not
        per statistic, is deprecated, and resets on stats update -- so the ratio is an estimate. Rows
        from this path are flagged ``is_estimate`` and badged in the report, so an approximate ratio
        is never read as a measured one.

A stale-stats finding on a database with auto-update-stats OFF is escalated by the analyzer: the
statistic being stale is the symptom, the setting is the cause.
"""

from __future__ import annotations

import pandas as pd

from .base import PerDatabaseCollector, ServerContext
from ..version import DatabaseInfo


class StatisticsCollector(PerDatabaseCollector):
    name = "statistics"
    tier = "daily"
    table = "stats_stale"

    def sql_file_for(self, ctx: ServerContext) -> str:
        if ctx.features.has_stats_properties:
            return "stats_age.sql"
        ctx.note(
            "statistics: sys.dm_db_stats_properties absent -- modification counts are an estimate "
            "from the deprecated per-table rowmodctr (last-updated dates remain exact)"
        )
        return "stats_age_legacy.sql"

    def databases(self, ctx: ServerContext) -> list[DatabaseInfo]:
        # A read-only database cannot have its statistics updated, so a finding there is noise.
        return [db for db in ctx.target_databases() if not db.is_read_only]

    def transform(self, rows: list[dict], ctx: ServerContext) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()
        is_estimate = not ctx.features.has_stats_properties
        records = [
            {
                "database_name": row.get("database_name"),
                "schema_name": row.get("schema_name"),
                "table_name": row.get("table_name"),
                "stats_name": row.get("stats_name"),
                "last_updated": row.get("last_updated"),
                "rows": _int(row.get("rows")),
                "modification_counter": _int(row.get("modification_counter")),
                "modification_ratio": _round(row.get("modification_ratio"), 3),
                "days_since_update": _int(row.get("days_since_update")),
                "no_recompute": bool(row.get("no_recompute")),
                "is_estimate": is_estimate,
            }
            for row in rows
        ]
        return pd.DataFrame(records)


def _int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _round(value, digits: int = 3) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None
