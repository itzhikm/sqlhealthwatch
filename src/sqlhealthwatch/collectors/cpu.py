"""CPU -- is this instance CPU-bound, and which queries are driving it.

Fast tier: one row per run holding SQL process CPU %, the other-process and idle split, the
signal-wait ratio (threads runnable but queued for a scheduler) and the runnable task count.

Daily tier: the top statements by total worker time, from the plan cache. This is deliberately the
plan cache even on Query Store instances -- Query Store only covers the databases where it is
enabled, whereas the CPU question is instance-wide.
"""

from __future__ import annotations

import pandas as pd

from ..util.text import prepare_statement_text
from .base import Collector, ServerContext, load_sql

# The ring buffer holds ~256 minutes of one-minute samples; the newest is the point-in-time value.
RING_BUFFER_SQL = "cpu_ring_buffer.sql"


class CpuCollector(Collector):
    name = "cpu"
    tier = "fast"
    table = "cpu_sample"

    def applies_to(self, ctx: ServerContext) -> bool:
        # The scheduler ring buffer and dm_os_schedulers are server-scoped and absent on Azure SQL DB.
        return ctx.features.is_box_product

    def fetch(self, ctx: ServerContext) -> list[dict]:
        conn = ctx.connection
        row: dict = {}

        if ctx.features.has_ring_buffer_cpu:
            ring = conn.query(load_sql(ctx.sql_dir, RING_BUFFER_SQL))
            if ring:
                newest = ring[0]
                row["sql_cpu_pct"] = newest.get("sql_cpu_pct")
                row["system_idle_pct"] = newest.get("system_idle_pct")
                row["other_process_pct"] = newest.get("other_process_pct")
        else:
            # 2005: the ring buffer XML has no ProcessUtilization node. Scheduler pressure still
            # reads, but the CPU percentages do not -- recorded as NULL rather than guessed at.
            ctx.note("CPU %: unavailable on this version (no ProcessUtilization in the ring buffer)")

        signal = conn.query_one(load_sql(ctx.sql_dir, "cpu_signal_wait.sql")) or {}
        row["signal_wait_pct"] = signal.get("signal_wait_pct")

        schedulers = conn.query_one(load_sql(ctx.sql_dir, "cpu_schedulers.sql")) or {}
        row["runnable_tasks"] = schedulers.get("runnable_tasks_now")

        return [row]

    def transform(self, rows: list[dict], ctx: ServerContext) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()
        row = rows[0]
        return pd.DataFrame(
            [
                {
                    "sql_cpu_pct": _pct(row.get("sql_cpu_pct")),
                    "other_process_pct": _pct(row.get("other_process_pct")),
                    "system_idle_pct": _pct(row.get("system_idle_pct")),
                    "signal_wait_pct": _round(row.get("signal_wait_pct"), 2),
                    "runnable_tasks": _int(row.get("runnable_tasks")),
                }
            ]
        )


class CpuTopQueriesCollector(Collector):
    """Daily: top statements by CPU, written to mon.query_top with rank_metric='cpu'.

    The query-history collector deliberately leaves the CPU ranking alone on the plan-cache path so
    the two never write the same rows twice.
    """

    name = "cpu_top_queries"
    tier = "daily"
    table = "query_top"
    sql_file = "cpu_top_queries.sql"

    def applies_to(self, ctx: ServerContext) -> bool:
        return ctx.features.is_box_product

    def transform(self, rows: list[dict], ctx: ServerContext) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()
        limit = ctx.settings.collection.top_n_queries
        mode = ctx.settings.collection.statement_text_mode
        records = []
        for row in rows[:limit]:
            executions = _int(row.get("execution_count")) or 0
            total_cpu_us = _float(row.get("total_cpu_us")) or 0.0
            avg_elapsed_us = _float(row.get("avg_elapsed_us")) or 0.0
            records.append(
                {
                    "source": "plan_cache",
                    "database_name": row.get("database_name"),
                    "query_identity": _hash_text(row.get("query_hash")),
                    "statement_text": prepare_statement_text(row.get("statement_text"), mode),
                    "executions": executions,
                    "total_duration_ms": round(avg_elapsed_us * executions / 1000.0, 2),
                    "avg_duration_ms": round(avg_elapsed_us / 1000.0, 2),
                    "max_duration_ms": None,
                    "total_cpu_ms": round(total_cpu_us / 1000.0, 2),
                    "total_logical_reads": _int(row.get("avg_logical_reads"), 0) * executions,
                    "rank_metric": "cpu",
                }
            )
        return pd.DataFrame(records)


def _pct(value) -> int | None:
    """Ring-buffer percentages are whole numbers; clamp defensively into the TINYINT column."""
    number = _int(value)
    if number is None:
        return None
    return max(0, min(100, number))


def _int(value, default=None) -> int | None:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round(value, digits: int) -> float | None:
    number = _float(value)
    return None if number is None else round(number, digits)


def _hash_text(value) -> str | None:
    """query_hash comes back as bytes from pyodbc; store it as the familiar 0x... form."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return "0x" + value.hex().upper()
    return str(value)
