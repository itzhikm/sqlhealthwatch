"""Retention and housekeeping.

Raw retention is 7 days. Because every sample table is clustered on
``(collected_at_utc, server_id)``, a prune is a range delete over contiguous data.

Two strategies, config-selected:

    delete            batched ``DELETE TOP (N)`` in a loop, so the transaction log stays small and
                      no single long-running delete blocks anything. Correct at this volume.
    partition_switch  switch out and drop the expired daily partition -- near-instant and minimally
                      logged. Overkill for 7 days at 40 servers, documented for scale.

Three tables are deliberately outside the 7-day rule:

    deadlock_event      rare, small and valuable for spotting a recurring hot path -- 90 days
    runs, server_status tiny rows that carry uptime/SLA reporting -- 30 days
    server, collector_watermark  permanent state, never pruned

There is no VACUUM here -- that is SQLite. The equivalent hygiene is keeping the repository in
SIMPLE recovery so the log does not grow during prunes, plus a weekly index rebuild and statistics
update on the monitoring database itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..config import RetentionConfig
from .repository import RAW_TABLES, Repository

log = logging.getLogger(__name__)


@dataclass
class PruneResult:
    deleted: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.deleted.values())

    def summary(self) -> str:
        if not self.deleted:
            return "nothing to prune"
        parts = [f"{table}={count}" for table, count in sorted(self.deleted.items()) if count]
        return ", ".join(parts) or "nothing to prune"


def prune(repo: Repository, config: RetentionConfig) -> PruneResult:
    """Apply every retention rule. Idempotent -- a second run in the same day deletes nothing."""
    result = PruneResult()

    for table in RAW_TABLES:
        time_column = "created_utc" if table == "findings" else "collected_at_utc"
        _prune_table(repo, config, table, time_column, config.raw_days, result)

    _prune_table(repo, config, "deadlock_event", "deadlock_time_utc", config.deadlock_days, result)
    _prune_table(repo, config, "server_status", "collected_at_utc", config.runs_days, result)
    _prune_table(repo, config, "alert_log", "sent_utc", config.runs_days, result)
    _prune_runs(repo, config, result)

    log.info("prune complete: %s", result.summary())
    return result


def _prune_table(repo: Repository, config: RetentionConfig, table: str, time_column: str,
                 days: int, result: PruneResult) -> None:
    try:
        if config.prune_strategy == "partition_switch":
            deleted = _prune_by_partition_switch(repo, table, time_column, days)
        else:
            deleted = _prune_by_batched_delete(repo, table, time_column, days, config.prune_batch_rows)
        result.deleted[table] = deleted
    except Exception as exc:
        log.warning("prune of %s failed: %s", table, exc)
        result.errors[table] = str(exc)


def _prune_by_batched_delete(repo: Repository, table: str, time_column: str, days: int,
                             batch_rows: int) -> int:
    """Delete in bounded batches so the log stays small and nothing blocks for long."""
    sql = (
        f"DELETE TOP (?) FROM {repo.table(table)} "
        f"WHERE {time_column} < DATEADD(day, -?, SYSUTCDATETIME())"
    )
    total = 0
    while True:
        deleted = repo.connection.execute(sql, [batch_rows, days])
        if deleted is None or deleted <= 0:
            break
        total += deleted
        if deleted < batch_rows:
            break
    return total


def _prune_by_partition_switch(repo: Repository, table: str, time_column: str, days: int) -> int:
    """Switch out and drop expired daily partitions.

    Requires the table to actually be range-partitioned by day. If it is not -- which is the default
    at this volume -- fall back to the batched delete rather than failing the prune.
    """
    partitioned = repo.connection.query_one(
        "SELECT COUNT(*) AS n FROM sys.indexes i "
        "JOIN sys.partition_schemes ps ON ps.data_space_id = i.data_space_id "
        "WHERE i.object_id = OBJECT_ID(?)",
        [f"{repo.schema}.{table}"],
    )
    if not partitioned or not partitioned.get("n"):
        log.debug("%s is not partitioned -- using the batched delete instead", table)
        return _prune_by_batched_delete(repo, table, time_column, days, 50000)

    # Partition switching is intentionally left as an explicit DBA operation: the staging table and
    # boundary maintenance are schema decisions, not something a prune job should invent at runtime.
    raise NotImplementedError(
        f"{table} is partitioned; switch out the expired boundary with a maintenance script, or set "
        f"retention.prune_strategy: delete"
    )


def _prune_runs(repo: Repository, config: RetentionConfig, result: PruneResult) -> None:
    """Run bookkeeping is kept longer than samples -- tiny rows, useful for uptime reporting."""
    try:
        deleted = repo.connection.execute(
            f"DELETE FROM {repo.table('runs')} WHERE started_utc < DATEADD(day, -?, SYSUTCDATETIME())",
            [config.runs_days],
        )
        result.deleted["runs"] = max(deleted or 0, 0)
    except Exception as exc:
        result.errors["runs"] = str(exc)


def rebuild_repository_indexes(repo: Repository) -> list[str]:
    """Weekly hygiene on the monitoring store itself.

    The repository takes constant append-and-delete traffic, so its own indexes and statistics
    degrade like any other database's. REORGANIZE (online) is used rather than REBUILD so this can
    run without a maintenance window.
    """
    actions: list[str] = []
    tables = repo.connection.query(
        "SELECT t.name FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id "
        "WHERE s.name = ?",
        [repo.schema],
    )
    for row in tables:
        table = row["name"]
        try:
            repo.connection.execute(f"ALTER INDEX ALL ON {repo.table(table)} REORGANIZE;", timeout_s=600)
            repo.connection.execute(f"UPDATE STATISTICS {repo.table(table)};", timeout_s=600)
            actions.append(table)
        except Exception as exc:
            log.warning("index maintenance on %s failed: %s", table, exc)
    log.info("repository index maintenance completed on %d table(s)", len(actions))
    return actions


def prune_exports(exports_dir, days: int) -> int:
    """Trim the cold Parquet/CSV archive, which is retained independently of the repository."""
    from datetime import date, timedelta
    from pathlib import Path

    root = Path(exports_dir)
    if not root.exists() or days <= 0:
        return 0
    cutoff = date.today() - timedelta(days=days)
    removed = 0
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            folder_date = date.fromisoformat(child.name)
        except ValueError:
            continue  # not a dated export folder -- leave it alone
        if folder_date < cutoff:
            for path in sorted(child.rglob("*"), reverse=True):
                path.rmdir() if path.is_dir() else path.unlink()
            child.rmdir()
            removed += 1
    return removed
