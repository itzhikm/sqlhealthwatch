"""Index optimization -- fragmentation, missing indexes, unused indexes.

Three collectors, three tables, one objective. All daily: the fragmentation scan is the heaviest
query in the project, which is why it runs LIMITED (never DETAILED) and only off-hours.

Everything here is a *suggestion*, and is stored as one. The missing-index DMV in particular
over-recommends, ignores write cost and does not notice that two of its suggestions overlap, so
``mon.index_missing`` rows are ranked estimates to review -- never something to apply as collected.
"""

from __future__ import annotations

import pandas as pd

from .base import Collector, PerDatabaseCollector, ServerContext


class IndexFragCollector(PerDatabaseCollector):
    name = "index_frag"
    tier = "daily"
    table = "index_frag"
    sql_file = "index_frag.sql"

    def transform(self, rows: list[dict], ctx: ServerContext) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()
        records = [
            {
                "database_name": row.get("database_name"),
                "schema_name": row.get("schema_name"),
                "table_name": row.get("table_name"),
                "index_name": row.get("index_name"),
                "index_type": row.get("index_type"),
                "avg_fragmentation_pct": _round(row.get("avg_fragmentation_pct")),
                "page_count": _int(row.get("page_count")),
                "recommendation": row.get("recommendation"),
            }
            for row in rows
        ]
        return pd.DataFrame(records)


class IndexMissingCollector(Collector):
    """Server-scoped: the missing-index DMV group covers every database at once."""

    name = "index_missing"
    tier = "daily"
    table = "index_missing"
    sql_file = "index_missing.sql"

    def applies_to(self, ctx: ServerContext) -> bool:
        return ctx.features.is_box_product

    def transform(self, rows: list[dict], ctx: ServerContext) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()
        records = [
            {
                "database_name": row.get("database_name"),
                "table_name": row.get("table_name"),
                "avg_user_impact": _round(row.get("avg_user_impact")),
                "demand": _int(row.get("demand")),
                "improvement_measure": _float(row.get("improvement_measure")),
                "equality_columns": row.get("equality_columns"),
                "inequality_columns": row.get("inequality_columns"),
                "included_columns": row.get("included_columns"),
            }
            for row in rows
        ]
        return pd.DataFrame(records)


class IndexUnusedCollector(PerDatabaseCollector):
    """Write-heavy, read-free nonclustered indexes -- drop candidates, read against uptime.

    ``dm_db_index_usage_stats`` resets on restart and, on some builds, on index rebuild. An index
    with zero reads on a box that restarted this morning means nothing, so these rows are only
    readable against the uptime recorded in mon.instance_meta for the same run.
    """

    name = "index_unused"
    tier = "daily"
    table = "index_unused"
    sql_file = "index_usage.sql"

    def transform(self, rows: list[dict], ctx: ServerContext) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()
        records = [
            {
                "database_name": row.get("database_name"),
                "schema_name": row.get("schema_name"),
                "table_name": row.get("table_name"),
                "index_name": row.get("index_name"),
                "reads": _int(row.get("reads")),
                "user_updates": _int(row.get("user_updates")),
                "last_user_seek": row.get("last_user_seek"),
            }
            for row in rows
        ]
        return pd.DataFrame(records)


class IndexColumnsCollector(PerDatabaseCollector):
    """Key and included column sets per nonclustered index.

    Stored so duplicate and overlapping indexes can be found from the repository: two indexes on the
    same table with identical key columns are redundant maintenance cost, and one whose keys are a
    leading prefix of another is usually mergeable into the wider index.
    """

    name = "index_columns"
    tier = "daily"
    table = "index_column"
    sql_file = "index_columns.sql"

    def transform(self, rows: list[dict], ctx: ServerContext) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(
            [
                {
                    "database_name": row.get("database_name"),
                    "schema_name": row.get("schema_name"),
                    "table_name": row.get("table_name"),
                    "index_name": row.get("index_name"),
                    "key_columns": row.get("key_columns"),
                    "included_columns": row.get("included_columns"),
                }
                for row in rows
            ]
        )


def _int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round(value, digits: int = 2) -> float | None:
    number = _float(value)
    return None if number is None else round(number, digits)
