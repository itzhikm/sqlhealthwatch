"""Blocking chains present at the sample instant.

Blocking is a *state*, so polling catches it while it lasts -- unlike a deadlock, which is an event
and is collected after the fact from system_health (see deadlocks.py). The 15-minute poll therefore
sees sustained blocking, not every brief episode; catching every episode needs the Blocked Process
Report, which requires a configuration change on the monitored instance and is opt-in per server.

Chain depth is computed here rather than in SQL: the DMV gives blocked -> blocker pairs, and walking
them in Python keeps the production query to a single cheap read.
"""

from __future__ import annotations

import pandas as pd

from ..util.text import prepare_statement_text
from .base import Collector, ServerContext

MAX_CHAIN_WALK = 50  # guards against a cycle in the reported pairs


class BlockingCollector(Collector):
    name = "blocking"
    tier = "fast"
    table = "blocking_event"
    sql_file = "blocking.sql"

    def applies_to(self, ctx: ServerContext) -> bool:
        return ctx.features.is_box_product

    def transform(self, rows: list[dict], ctx: ServerContext) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()

        mode = ctx.settings.collection.statement_text_mode
        blocked_by = {
            _int(row.get("blocked_spid")): _int(row.get("blocking_spid"))
            for row in rows
            if row.get("blocked_spid") is not None
        }

        records = [
            {
                "blocked_spid": _int(row.get("blocked_spid")),
                "blocking_spid": _int(row.get("blocking_spid")),
                "wait_type": row.get("wait_type"),
                "wait_seconds": _round(row.get("wait_seconds")),
                "database_name": row.get("database_name"),
                "blocked_stmt": prepare_statement_text(row.get("blocked_stmt"), mode),
                "chain_depth": chain_depth(_int(row.get("blocked_spid")), blocked_by),
            }
            for row in rows
        ]
        return pd.DataFrame(records)


def chain_depth(spid: int | None, blocked_by: dict[int | None, int | None]) -> int:
    """How many sessions deep this one sits in the blocking chain (head = 1)."""
    depth, seen, current = 1, set(), spid
    while current in blocked_by and blocked_by[current] and current not in seen:
        seen.add(current)
        current = blocked_by[current]
        depth += 1
        if depth > MAX_CHAIN_WALK:
            break
    return depth


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
