"""Query history -- top queries by duration, CPU, reads and execution count.

Primary path is Query Store (2016+, per database where it is enabled): it persists across restarts,
so it genuinely answers "how often did this run and how long", and day-over-day comparison keys on
the stable ``query_id``. Where Query Store is unavailable or off, the plan-cache fallback in
``plan_cache.py`` is used instead -- an expected path on a mixed fleet, and labelled as such.

One collector owns ``mon.query_top`` so a query is never counted twice. The one deliberate
exception is the CPU ranking on the plan-cache path: that belongs to ``cpu.CpuTopQueriesCollector``,
which runs the CPU-specific query with database attribution, so this collector leaves it out there.
"""

from __future__ import annotations

import pandas as pd

from . import plan_cache
from ..util.text import prepare_statement_text
from .base import Collector, ServerContext, load_sql

RANKS = ["duration", "cpu", "reads", "exec"]

# ORDER BY expression per ranked metric for the Query Store shape.
QS_ORDER_BY = {
    "duration": "total_duration_ms",
    "cpu": "total_cpu_ms",
    "reads": "total_logical_reads",
    "exec": "executions",
}


class QueryHistoryCollector(Collector):
    name = "query_history"
    tier = "daily"
    table = "query_top"

    def uses_query_store(self, ctx: ServerContext) -> bool:
        return bool(ctx.features.query_store_databases)

    def fetch(self, ctx: ServerContext) -> list[dict]:
        if self.uses_query_store(ctx):
            return self._fetch_query_store(ctx)
        # CPU ranking on this path is collected by cpu.CpuTopQueriesCollector, which attributes
        # queries to a database; collecting it here as well would duplicate the rows.
        ctx.note(
            "query history: plan cache (not durable -- cleared on restart, memory pressure and recompile)"
        )
        return plan_cache.fetch_ranked(ctx, [r for r in RANKS if r != "cpu"])

    def _fetch_query_store(self, ctx: ServerContext) -> list[dict]:
        template = load_sql(ctx.sql_dir, "querystore_top.sql")
        hours = ctx.settings.collection.query_window_hours
        databases = ctx.features.query_store_databases
        skipped = len(ctx.features.databases) - len(databases)
        if skipped > 0:
            ctx.note(
                f"query history: Query Store covers {len(databases)} of {len(ctx.features.databases)} "
                f"databases; the rest have no durable history"
            )

        rows: list[dict] = []
        for db in databases:
            for rank in RANKS:
                sql = template.replace("{order_by}", QS_ORDER_BY[rank])
                try:
                    result = ctx.connection.query_in_database(db.name, sql, [hours])
                except Exception as exc:
                    ctx.note(f"query history: skipped database {db.name} ({exc})")
                    break
                for row in result:
                    rows.append({**row, "rank_metric": rank, "source": "query_store"})
        return rows

    def transform(self, rows: list[dict], ctx: ServerContext) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()

        limit = ctx.settings.collection.top_n_queries
        mode = ctx.settings.collection.statement_text_mode

        records = []
        for row in rows:
            statement = row.get("query_sql_text") or row.get("statement_text")
            identity = row.get("query_id")
            if identity is None:
                identity = row.get("query_identity")
            records.append(
                {
                    "source": row.get("source"),
                    "database_name": row.get("database_name"),
                    "query_identity": None if identity is None else str(identity)[:64],
                    "statement_text": prepare_statement_text(statement, mode),
                    "executions": _int(row.get("executions")),
                    "total_duration_ms": _round(row.get("total_duration_ms")),
                    "avg_duration_ms": _round(row.get("avg_duration_ms")),
                    "max_duration_ms": _round(row.get("max_duration_ms")),
                    "total_cpu_ms": _round(row.get("total_cpu_ms")),
                    "total_logical_reads": _int(row.get("total_logical_reads")),
                    "rank_metric": row.get("rank_metric"),
                }
            )

        frame = pd.DataFrame(records)
        # Keep each ranking's top-N, per database, after normalization.
        return (
            frame.groupby(["rank_metric", "database_name"], dropna=False, group_keys=False)
            .head(limit)
            .reset_index(drop=True)
        )


def _int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _round(value, digits: int = 2) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None
