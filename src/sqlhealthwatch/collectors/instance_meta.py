"""Instance metadata -- uptime, capacity, configuration, per-database flags.

This is the collector that makes every other one readable. Almost every DMV in the project is
cumulative since restart, so ``sqlserver_start_time`` is what separates "this box has a wait
problem" from "this box has been up for 400 days".

Runs on the daily tier and on the first fast run after the collector starts, so a freshly patched or
restarted instance is re-detected within a day.
"""

from __future__ import annotations

import pandas as pd

from .base import Collector, ServerContext


class InstanceMetaCollector(Collector):
    name = "instance_meta"
    tier = "daily"
    table = "instance_meta"

    def sql_file_for(self, ctx: ServerContext) -> str:
        # 2005 has no sqlserver_start_time on dm_os_sys_info; SPID 1's login time is equivalent.
        return "instance_meta.sql" if ctx.features.has_sys_info_start_time else "instance_meta_legacy.sql"

    def transform(self, rows: list[dict], ctx: ServerContext) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()
        row = rows[0]
        databases = ctx.features.databases
        auto_update_off = sum(1 for db in databases if not db.is_auto_update_stats_on)
        return pd.DataFrame(
            [
                {
                    "sqlserver_start_time": row.get("sqlserver_start_time"),
                    "uptime_minutes": _int(row.get("uptime_minutes")),
                    "cpu_count": _int(row.get("cpu_count")),
                    "scheduler_count": _int(row.get("scheduler_count")),
                    "max_server_memory_mb": _int(row.get("max_server_memory_mb")),
                    "min_server_memory_mb": _int(row.get("min_server_memory_mb")),
                    "maxdop": _int(row.get("maxdop")),
                    "cost_threshold": _int(row.get("cost_threshold")),
                    "blocked_process_threshold_s": _int(row.get("blocked_process_threshold_s")),
                    "database_count": len(databases),
                    "auto_update_stats_off_count": auto_update_off,
                }
            ]
        )


def _int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
