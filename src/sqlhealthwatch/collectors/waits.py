"""Top waits -- the context that makes the CPU, memory and IO numbers interpretable.

Cumulative since restart, like every other wait DMV: a large RESOURCE_SEMAPHORE total on a box that
has been up for a year says much less than the same total on one restarted yesterday. The report
always shows uptime beside these.
"""

from __future__ import annotations

import pandas as pd

from .base import Collector, ServerContext


class WaitsCollector(Collector):
    name = "waits"
    tier = "fast"
    table = "wait_sample"
    sql_file = "waits.sql"

    def applies_to(self, ctx: ServerContext) -> bool:
        return ctx.features.is_box_product

    def transform(self, rows: list[dict], ctx: ServerContext) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()
        records = [
            {
                "wait_type": row.get("wait_type"),
                "wait_time_ms": _int(row.get("wait_time_ms")),
                "resource_wait_ms": _int(row.get("resource_wait_ms")),
                "signal_wait_time_ms": _int(row.get("signal_wait_time_ms")),
                "waiting_tasks_count": _int(row.get("waiting_tasks_count")),
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
