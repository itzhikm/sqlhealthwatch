"""The collector registry.

Adding a metric is: one collector module, one table in ``storage/schema.sql``, one threshold entry
-- and one line here. Nothing else in the project needs to change.
"""

from __future__ import annotations

from .base import Collector, PerDatabaseCollector, ServerContext, Tier
from .blocking import BlockingCollector
from .cpu import CpuCollector, CpuTopQueriesCollector
from .deadlocks import DeadlockCollector
from .indexes import (
    IndexColumnsCollector,
    IndexFragCollector,
    IndexMissingCollector,
    IndexUnusedCollector,
)
from .instance_meta import InstanceMetaCollector
from .io_disk import IoDiskCollector, TempdbCollector
from .memory import MemoryClerkCollector, MemoryCollector
from .query_store import QueryHistoryCollector
from .space import SpaceDbCollector, SpaceDriveCollector
from .statistics import StatisticsCollector
from .waits import WaitsCollector

ALL_COLLECTORS: list[Collector] = [
    # ---- fast tier: every 15 minutes, lightweight, alertable
    CpuCollector(),
    MemoryCollector(),
    IoDiskCollector(),
    WaitsCollector(),
    BlockingCollector(),
    SpaceDriveCollector(),
    # ---- daily tier: off-hours, heavier, digested into the report
    InstanceMetaCollector(),
    SpaceDbCollector(),
    TempdbCollector(),
    MemoryClerkCollector(),
    IndexFragCollector(),
    IndexMissingCollector(),
    IndexUnusedCollector(),
    IndexColumnsCollector(),
    StatisticsCollector(),
    QueryHistoryCollector(),
    CpuTopQueriesCollector(),
    DeadlockCollector(),
]


def collectors_for(tier: Tier) -> list[Collector]:
    return [c for c in ALL_COLLECTORS if c.tier == tier]


def get(name: str) -> Collector | None:
    return next((c for c in ALL_COLLECTORS if c.name == name), None)


__all__ = [
    "ALL_COLLECTORS",
    "Collector",
    "PerDatabaseCollector",
    "ServerContext",
    "collectors_for",
    "get",
]
