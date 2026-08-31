"""File-level IO latency and throughput.

``dm_io_virtual_file_stats`` is cumulative since restart, so the since-restart averages this
collector stores are only context. The numbers that matter -- interval latency and MB/s over the
last 15 minutes -- are computed by ``analyze/derive.py`` from two consecutive samples, which is why
a spike shows up as a window rate instead of being flattened into a months-long average.
"""

from __future__ import annotations

import pandas as pd

from .base import Collector, ServerContext


class IoDiskCollector(Collector):
    name = "io_disk"
    tier = "fast"
    table = "io_file_sample"
    sql_file = "io_file_stats.sql"

    def applies_to(self, ctx: ServerContext) -> bool:
        return ctx.features.is_box_product

    def transform(self, rows: list[dict], ctx: ServerContext) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()
        records = [
            {
                "database_name": row.get("database_name"),
                "file_type": _file_type(row.get("file_type")),
                "physical_name": row.get("physical_name"),
                "num_of_reads": _int(row.get("num_of_reads")),
                "num_of_writes": _int(row.get("num_of_writes")),
                "bytes_read": _int(row.get("num_of_bytes_read")),
                "bytes_written": _int(row.get("num_of_bytes_written")),
                "io_stall_read_ms": _int(row.get("io_stall_read_ms")),
                "io_stall_write_ms": _int(row.get("io_stall_write_ms")),
                "avg_read_latency_ms": _round(row.get("avg_read_latency_ms")),
                "avg_write_latency_ms": _round(row.get("avg_write_latency_ms")),
                # Interval columns are filled by derive.py once the previous sample is available.
                "interval_read_latency_ms": None,
                "interval_write_latency_ms": None,
                "interval_read_mb_s": None,
                "interval_write_mb_s": None,
            }
            for row in rows
        ]
        return pd.DataFrame(records)


class TempdbCollector(Collector):
    """tempdb allocation split -- user vs internal objects vs version store."""

    name = "tempdb"
    tier = "daily"
    table = "tempdb_sample"
    sql_file = "tempdb_usage.sql"

    def applies_to(self, ctx: ServerContext) -> bool:
        return ctx.features.is_box_product

    def transform(self, rows: list[dict], ctx: ServerContext) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()
        row = rows[0]
        return pd.DataFrame(
            [
                {
                    "total_mb": _int(row.get("total_mb")),
                    "free_mb": _int(row.get("free_mb")),
                    "user_object_mb": _int(row.get("user_object_mb")),
                    "internal_object_mb": _int(row.get("internal_object_mb")),
                    "version_store_mb": _int(row.get("version_store_mb")),
                }
            ]
        )


def _file_type(value) -> str | None:
    """type_desc is ROWS / LOG / FILESTREAM; the column is VARCHAR(8)."""
    return None if value is None else str(value)[:8]


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
