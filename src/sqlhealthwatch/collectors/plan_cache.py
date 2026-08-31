"""Plan-cache query history -- the fallback path for the query-history objective.

Used on any pre-2016 instance, and on 2016+ instances where Query Store is not enabled on any
database. On a mixed fleet this is a normal path for a sizeable chunk of the servers, not a rare
edge case.

What it cannot do: the plan cache is volatile. It clears on restart, under memory pressure and on
recompile, so there is no durable history here -- day-over-day means diffing one daily snapshot
against the previous one, keyed on ``query_hash``, whose identity is weaker than Query Store's
``query_id``. Rows are labelled ``source='plan_cache'`` so the report never compares them with
Query Store rows as equals.
"""

from __future__ import annotations

from .base import ServerContext, load_sql

# ORDER BY expression per ranked metric, substituted into the query's {order_by} placeholder.
# Values are fixed here, never taken from configuration or user input.
RANK_ORDER_BY = {
    "duration": "total_duration_ms",
    "cpu": "total_cpu_ms",
    "reads": "total_logical_reads",
    "exec": "executions",
}


def sql_file_for(ctx: ServerContext) -> str:
    """2005 has no query_hash column, so identity falls back to sql_handle + statement offset."""
    if ctx.features.has_query_hash:
        return "plan_cache_top.sql"
    ctx.note("query history: no query_hash on this version -- query identity is coarser")
    return "plan_cache_top_legacy.sql"


def fetch_ranked(ctx: ServerContext, ranks: list[str]) -> list[dict]:
    """Run the plan-cache query once per ranking so each top-N is a true top-N.

    Re-sorting a single top-25-by-duration result would silently answer a different question than
    "the 25 heaviest queries by CPU".
    """
    template = load_sql(ctx.sql_dir, sql_file_for(ctx))
    rows: list[dict] = []
    for rank in ranks:
        sql = template.replace("{order_by}", RANK_ORDER_BY[rank])
        for row in ctx.connection.query(sql):
            rows.append({**row, "rank_metric": rank, "source": "plan_cache", "database_name": None})
    return rows
