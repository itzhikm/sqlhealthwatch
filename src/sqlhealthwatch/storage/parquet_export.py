"""Cold export archive.

The repository is the source of truth; these files are an optional portable archive. Partitioning by
date keeps them small enough for later pandas or DuckDB analysis, and they are retained
independently of the 7-day repository rule. Set ``collection.parquet_export: false`` to skip them.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)


def export_frame(frame: pd.DataFrame, exports_dir: Path, run_date: str, tier: str, collector: str,
                 run_id: str, write_csv: bool = False) -> Path | None:
    """Write one collector's frame for one run to ``data/exports/{date}/{tier}/``."""
    if frame is None or frame.empty:
        return None

    target_dir = Path(exports_dir) / run_date / tier
    target_dir.mkdir(parents=True, exist_ok=True)
    short_run = str(run_id).split("-")[0]
    path = target_dir / f"{collector}_{short_run}.parquet"

    try:
        frame.to_parquet(path, index=False)
    except Exception as exc:
        # A failed export must never fail a collection run -- the repository already has the data.
        log.warning("parquet export of %s failed: %s", collector, exc)
        return None

    if write_csv:
        try:
            frame.to_csv(path.with_suffix(".csv"), index=False, encoding="utf-8")
        except Exception as exc:
            log.warning("csv export of %s failed: %s", collector, exc)
    return path
