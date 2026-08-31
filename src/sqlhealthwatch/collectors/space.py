"""Free space -- per database file and per volume.

The drive collector is the clearest case of version gating in the project. ``dm_os_volume_stats``
(2008 R2 SP1+) gives total, free and free %. Where it is absent the fallback is ``xp_fixeddrives``,
which returns *free MB only* -- no total, so no percentage. Rather than invent a denominator, the
legacy path stores ``free_gb`` and leaves ``total_gb`` / ``free_pct`` NULL, and the analyzer
switches that server to an absolute free-MB threshold. The report badges it so an empty free-%
column is never read as "healthy".
"""

from __future__ import annotations

import pandas as pd

from .base import Collector, PerDatabaseCollector, ServerContext


class SpaceDriveCollector(Collector):
    name = "space_drive"
    tier = "fast"
    table = "space_drive_sample"

    def applies_to(self, ctx: ServerContext) -> bool:
        return ctx.features.is_box_product

    def sql_file_for(self, ctx: ServerContext) -> str:
        if ctx.features.has_volume_stats:
            return "space_drive.sql"
        ctx.note(
            "drive space: sys.dm_os_volume_stats absent -- using xp_fixeddrives "
            "(free MB only, no total and no free %)"
        )
        return "space_drive_legacy.sql"

    def transform(self, rows: list[dict], ctx: ServerContext) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()
        records = [
            {
                "volume_mount_point": row.get("volume_mount_point"),
                "total_gb": _round(row.get("total_gb")),
                "free_gb": _round(row.get("free_gb")),
                "free_pct": _round(row.get("free_pct")),
            }
            for row in rows
        ]
        return pd.DataFrame(records).drop_duplicates(subset=["volume_mount_point"])


class SpaceDbCollector(PerDatabaseCollector):
    """Per-file used/free space. Feeds both the free-% threshold and the days-to-full projection."""

    name = "space_db"
    tier = "daily"
    table = "space_db_sample"
    sql_file = "space_db.sql"

    def transform(self, rows: list[dict], ctx: ServerContext) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()
        records = []
        for row in rows:
            size_mb = _int(row.get("size_mb"))
            free_mb = _int(row.get("free_mb"))
            free_pct = round(free_mb * 100.0 / size_mb, 2) if size_mb and free_mb is not None else None
            records.append(
                {
                    "database_name": row.get("database_name"),
                    "logical_name": row.get("logical_name"),
                    "file_type": _file_type(row.get("file_type")),
                    "size_mb": size_mb,
                    "used_mb": _int(row.get("used_mb")),
                    "free_mb": free_mb,
                    "free_pct": free_pct,
                    "max_size_mb": _int(row.get("max_size_mb")),
                    "is_percent_growth": bool(row.get("is_percent_growth")),
                }
            )
        return pd.DataFrame(records)


def _file_type(value) -> str | None:
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
