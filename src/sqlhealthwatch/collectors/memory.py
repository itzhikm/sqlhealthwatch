"""Memory pressure -- buffer pool health, pending grants, cache churn.

The flat "PLE 300" rule is obsolete: the floor is computed per instance from the buffer pool size
(see :func:`analyze.derive.dynamic_ple_floor`), and on NUMA boxes the *lowest node* PLE is kept
alongside the instance figure, because a starved node hides inside a healthy-looking aggregate.
"""

from __future__ import annotations

import pandas as pd

from ..analyze.derive import dynamic_ple_floor
from .base import Collector, ServerContext, load_sql

PLE = "Page life expectancy"
CACHE_HIT = "Buffer cache hit ratio"
CACHE_HIT_BASE = "Buffer cache hit ratio base"
GRANTS_PENDING = "Memory Grants Pending"
TOTAL_MEMORY_KB = "Total Server Memory (KB)"
TARGET_MEMORY_KB = "Target Server Memory (KB)"


class MemoryCollector(Collector):
    name = "memory"
    tier = "fast"
    table = "memory_sample"

    def sql_file_for(self, ctx: ServerContext) -> str:
        # The `Buffer Node` counter object (per-NUMA-node PLE) does not exist before 2008.
        return "perf_counters.sql" if ctx.features.has_buffer_node_ple else "perf_counters_legacy.sql"

    def transform(self, rows: list[dict], ctx: ServerContext) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()

        counters = _index_counters(rows)
        instance_ple = _first(counters.get(PLE, {}).values())
        node_ples = [v for k, v in counters.get(PLE, {}).items() if k and k.lower().startswith("node")]

        total_kb = _first(counters.get(TOTAL_MEMORY_KB, {}).values())
        target_kb = _first(counters.get(TARGET_MEMORY_KB, {}).values())
        total_mb = int(total_kb / 1024) if total_kb else None
        target_mb = int(target_kb / 1024) if target_kb else None

        hit = _first(counters.get(CACHE_HIT, {}).values())
        hit_base = _first(counters.get(CACHE_HIT_BASE, {}).values())
        # The counter is a ratio pair, not a percentage -- it only means anything divided by its base.
        hit_ratio = round(hit * 100.0 / hit_base, 2) if hit is not None and hit_base else None

        return pd.DataFrame(
            [
                {
                    "page_life_expectancy": _int(instance_ple),
                    "ple_dynamic_floor": dynamic_ple_floor(target_mb),
                    "min_node_ple": _int(min(node_ples)) if node_ples else None,
                    "memory_grants_pending": _int(_first(counters.get(GRANTS_PENDING, {}).values())),
                    "buffer_cache_hit_ratio": hit_ratio,
                    "total_server_memory_mb": total_mb,
                    "target_server_memory_mb": target_mb,
                }
            ]
        )


class MemoryClerkCollector(Collector):
    """Daily context: where memory is actually going.

    Not a threshold source -- it exists so a low-PLE finding can be read next to the clerk that grew,
    by querying mon.memory_clerk directly.
    """

    name = "memory_clerks"
    tier = "daily"
    table = "memory_clerk"

    def sql_file_for(self, ctx: ServerContext) -> str:
        # pages_kb replaced the single_pages_kb / multi_pages_kb columns in 2012.
        return "memory_clerks.sql" if ctx.features.has_pages_kb_clerks else "memory_clerks_legacy.sql"

    def transform(self, rows: list[dict], ctx: ServerContext) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(
            [
                {"clerk_type": row.get("clerk_type"), "pages_mb": _int(row.get("pages_mb"))}
                for row in rows
            ]
        )


def _index_counters(rows: list[dict]) -> dict[str, dict[str, float]]:
    """{counter_name: {instance_name: value}} -- instance_name separates NUMA nodes."""
    indexed: dict[str, dict[str, float]] = {}
    for row in rows:
        name = (row.get("counter_name") or "").strip()
        instance = (row.get("instance_name") or "").strip()
        value = row.get("cntr_value")
        if value is None:
            continue
        indexed.setdefault(name, {})[instance] = float(value)
    return indexed


def _first(values):
    for value in values:
        return value
    return None


def _int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
