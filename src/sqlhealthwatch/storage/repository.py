"""The central SQL Server repository (``DBA_Monitoring``).

This is the collector's output and the source of truth; the Parquet export is a cold archive.
Unlike SQLite there is no single-writer limit, so the per-server workers each bulk-insert their own
rows concurrently -- one transaction per collector per server, append-only, batched with
``fast_executemany``.

Two guards live here rather than in the runner because they are repository state:

    * the per-tier application lock, so a slow run is skipped rather than piled onto
    * the ``server_id`` cache, so the hot path never re-resolves the dimension row
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

import pandas as pd

from ..config import RepositoryConfig, Settings
from ..connection import SqlConnection, connect
from ..util.timeutil import utcnow

log = logging.getLogger(__name__)

SCHEMA_FILE = Path(__file__).with_name("schema.sql")

# Sample tables pruned on the standard raw-retention horizon.
RAW_TABLES = [
    "cpu_sample",
    "memory_sample",
    "io_file_sample",
    "space_db_sample",
    "space_drive_sample",
    "tempdb_sample",
    "wait_sample",
    "blocking_event",
    "index_frag",
    "index_missing",
    "index_unused",
    "index_column",
    "memory_clerk",
    "stats_stale",
    "query_top",
    "instance_meta",
    "findings",
]


class RepositoryError(RuntimeError):
    pass


class Repository:
    """Connection, schema bootstrap, bulk write, and the reads the analyzer needs."""

    def __init__(self, config: RepositoryConfig, settings: Settings | None = None) -> None:
        self.config = config
        self.settings = settings
        self.schema = config.schema_name
        self._conn: SqlConnection | None = None
        self._server_ids: dict[str, int] = {}

    # ------------------------------------------------------------------------------ lifecycle

    def connect(self) -> "Repository":
        self._conn = connect(self.config, database=self.config.database)
        return self

    @property
    def connection(self) -> SqlConnection:
        if self._conn is None:
            raise RepositoryError("repository is not connected -- call connect() first")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Repository":
        return self.connect()

    def __exit__(self, *exc_info) -> None:
        self.close()

    def table(self, name: str) -> str:
        return f"[{self.schema}].[{name}]"

    # ------------------------------------------------------------------------------ bootstrap

    def bootstrap(self) -> None:
        """Apply schema.sql idempotently. Needs db_ddladmin, which is dropped after first run."""
        script = SCHEMA_FILE.read_text(encoding="utf-8").replace("{schema}", self.schema)
        for batch in _split_batches(script):
            self.connection.execute(batch)
        log.info("repository schema verified in %s.%s", self.config.database, self.schema)

    def schema_exists(self) -> bool:
        row = self.connection.query_one(
            "SELECT COUNT(*) AS n FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id "
            "WHERE s.name = ?",
            [self.schema],
        )
        return bool(row and row.get("n"))

    # -------------------------------------------------------------------------- overlap guard

    @contextmanager
    def tier_lock(self, tier: str):
        """Session-scoped application lock per tier.

        If the previous run of the same tier is still going, the new one logs and skips rather than
        piling a second full fan-out onto the fleet.
        """
        resource = f"sqlhealthwatch_{tier}"
        row = self.connection.query_one(
            "DECLARE @rc INT; "
            "EXEC @rc = sp_getapplock @Resource = ?, @LockMode = 'Exclusive', "
            "@LockOwner = 'Session', @LockTimeout = 0; "
            "SELECT @rc AS rc;",
            [resource],
        )
        acquired = bool(row) and int(row.get("rc", -1)) >= 0
        try:
            yield acquired
        finally:
            if acquired:
                try:
                    self.connection.execute(
                        "EXEC sp_releaseapplock @Resource = ?, @LockOwner = 'Session';", [resource]
                    )
                except Exception as exc:  # pragma: no cover - best effort on shutdown
                    log.warning("could not release the %s tier lock: %s", tier, exc)

    # ------------------------------------------------------------------------------ dimension

    def ensure_server(self, name: str, host: str | None = None, tags: Sequence[str] = ()) -> int:
        """Resolve (creating if needed) the compact server_id that every sample row carries."""
        if name in self._server_ids:
            return self._server_ids[name]

        row = self.connection.query_one(
            f"SELECT server_id FROM {self.table('server')} WHERE server_name = ?", [name]
        )
        if row is None:
            self.connection.execute(
                f"INSERT INTO {self.table('server')} (server_name, host_name, tags) VALUES (?, ?, ?)",
                [name, host, ",".join(tags) or None],
            )
            row = self.connection.query_one(
                f"SELECT server_id FROM {self.table('server')} WHERE server_name = ?", [name]
            )
        if row is None:  # pragma: no cover - only if the insert silently failed
            raise RepositoryError(f"could not resolve server_id for {name}")

        server_id = int(row["server_id"])
        self._server_ids[name] = server_id
        return server_id

    def update_server_features(self, server_id: int, features) -> None:
        """Cache the probed feature set on the dimension row, refreshed on every run."""
        self.connection.execute(
            f"UPDATE {self.table('server')} SET product_version = ?, major_version = ?, "
            f"minor_version = ?, product_level = ?, edition = ?, engine_edition = ?, "
            f"feature_flags = ?, features_checked_utc = ? WHERE server_id = ?",
            [
                features.product_version,
                features.major_version,
                features.minor_version,
                features.product_level,
                features.edition,
                features.engine_edition,
                features.flags_json(),
                utcnow(),
                server_id,
            ],
        )

    def servers(self) -> pd.DataFrame:
        return pd.DataFrame(self.connection.query(f"SELECT * FROM {self.table('server')}"))

    # ----------------------------------------------------------------------------- run record

    def start_run(self, tier: str) -> str:
        run_id = str(uuid.uuid4())
        self.connection.execute(
            f"INSERT INTO {self.table('runs')} (run_id, tier, started_utc) VALUES (?, ?, ?)",
            [run_id, tier, utcnow()],
        )
        return run_id

    def finish_run(self, run_id: str, ok: int, failed: int, notes: str | None = None) -> None:
        self.connection.execute(
            f"UPDATE {self.table('runs')} SET finished_utc = ?, servers_ok = ?, servers_failed = ?, "
            f"notes = ? WHERE run_id = ?",
            [utcnow(), ok, failed, (notes or None), run_id],
        )

    def record_server_status(
        self, run_id: str, server_id: int, ok: bool, duration_ms: int, error: str | None = None
    ) -> None:
        self.connection.execute(
            f"INSERT INTO {self.table('server_status')} "
            f"(run_id, server_id, ok, error, duration_ms, collected_at_utc) VALUES (?, ?, ?, ?, ?, ?)",
            [run_id, server_id, 1 if ok else 0, (error or None), duration_ms, utcnow()],
        )

    # ---------------------------------------------------------------------------- bulk writes

    def write(self, table: str, frame: pd.DataFrame) -> int:
        """Batched append-only insert. Returns the number of rows written."""
        if frame is None or frame.empty or not table:
            return 0

        columns = list(frame.columns)
        placeholders = ", ".join("?" for _ in columns)
        column_list = ", ".join(f"[{c}]" for c in columns)
        sql = f"INSERT INTO {self.table(table)} ({column_list}) VALUES ({placeholders})"

        rows = _to_tuples(frame)
        batch_size = self.config.bulk.batch_rows
        written = 0

        cursor = self.connection._conn.cursor()  # noqa: SLF001 - fast_executemany is cursor-level
        try:
            cursor.fast_executemany = self.config.bulk.fast_executemany
            cursor.timeout = self.config.query_timeout_s
            for start in range(0, len(rows), batch_size):
                chunk = rows[start : start + batch_size]
                cursor.executemany(sql, chunk)
                written += len(chunk)
        finally:
            cursor.close()
        return written

    def write_findings(self, findings: Iterable) -> int:
        rows = [f.as_row() for f in findings]
        if not rows:
            return 0
        return self.write("findings", pd.DataFrame(rows))

    def log_alert(self, server_id: int, fingerprint: str, severity: str, channel: str, ok: bool,
                  error: str | None = None) -> None:
        self.connection.execute(
            f"INSERT INTO {self.table('alert_log')} "
            f"(server_id, fingerprint, severity, channel, sent_utc, ok, error) VALUES (?,?,?,?,?,?,?)",
            [server_id, fingerprint[:200], severity, channel, utcnow(), 1 if ok else 0, (error or None)[:1000] if error else None],
        )

    # ---------------------------------------------------------------------------- watermarks

    def get_watermark(self, server_id: int, collector: str) -> datetime | None:
        row = self.connection.query_one(
            f"SELECT last_value_utc FROM {self.table('collector_watermark')} "
            f"WHERE server_id = ? AND collector = ?",
            [server_id, collector],
        )
        return row.get("last_value_utc") if row else None

    def set_watermark(self, server_id: int, collector: str, value: datetime | None) -> None:
        if value is None:
            return
        self.connection.execute(
            f"MERGE {self.table('collector_watermark')} AS target "
            f"USING (SELECT ? AS server_id, ? AS collector) AS src "
            f"  ON target.server_id = src.server_id AND target.collector = src.collector "
            f"WHEN MATCHED THEN UPDATE SET last_value_utc = ?, updated_utc = ? "
            f"WHEN NOT MATCHED THEN INSERT (server_id, collector, last_value_utc, updated_utc) "
            f"  VALUES (?, ?, ?, ?);",
            [server_id, collector, value, utcnow(), server_id, collector, value, utcnow()],
        )

    # ------------------------------------------------------------------- reads for the analyzer

    def recent_values(self, server_id: int, table: str, column: str, limit: int = 8) -> list[float | None]:
        """Last N values of one column, oldest first -- the input to sustained-average rules."""
        rows = self.connection.query(
            f"SELECT TOP (?) {column} AS value FROM {self.table(table)} "
            f"WHERE server_id = ? ORDER BY collected_at_utc DESC",
            [limit, server_id],
        )
        return [row.get("value") for row in reversed(rows)]

    def previous_io_sample(self, server_id: int) -> pd.DataFrame:
        """The most recent complete io_file_sample set, for interval rate derivation."""
        rows = self.connection.query(
            f"SELECT * FROM {self.table('io_file_sample')} WHERE server_id = ? AND collected_at_utc = ("
            f"  SELECT MAX(collected_at_utc) FROM {self.table('io_file_sample')} WHERE server_id = ?)",
            [server_id, server_id],
        )
        return pd.DataFrame(rows)

    def growth_series(self, server_id: int, days: int = 7) -> pd.DataFrame:
        """Per-file used/size history over the retained window, for the days-to-full projection."""
        rows = self.connection.query(
            f"SELECT collected_at_utc, database_name, logical_name, used_mb, size_mb, max_size_mb "
            f"FROM {self.table('space_db_sample')} "
            f"WHERE server_id = ? AND collected_at_utc >= DATEADD(day, -?, SYSUTCDATETIME()) "
            f"ORDER BY collected_at_utc",
            [server_id, days],
        )
        return pd.DataFrame(rows)

    def drive_growth_series(self, server_id: int, days: int = 7) -> pd.DataFrame:
        rows = self.connection.query(
            f"SELECT collected_at_utc, volume_mount_point, total_gb, free_gb "
            f"FROM {self.table('space_drive_sample')} "
            f"WHERE server_id = ? AND collected_at_utc >= DATEADD(day, -?, SYSUTCDATETIME()) "
            f"ORDER BY collected_at_utc",
            [server_id, days],
        )
        return pd.DataFrame(rows)

    def deadlock_count(self, server_id: int, hours: int = 24) -> int:
        row = self.connection.query_one(
            f"SELECT COUNT(*) AS n FROM {self.table('deadlock_event')} "
            f"WHERE server_id = ? AND deadlock_time_utc >= DATEADD(hour, -?, SYSUTCDATETIME())",
            [server_id, hours],
        )
        return int(row.get("n", 0)) if row else 0

    def last_alert_time(self, fingerprint: str) -> datetime | None:
        """Drives the alert cooldown: when this exact finding was last pushed."""
        row = self.connection.query_one(
            f"SELECT MAX(sent_utc) AS sent FROM {self.table('alert_log')} "
            f"WHERE fingerprint = ? AND ok = 1",
            [fingerprint[:200]],
        )
        return row.get("sent") if row else None

    # ------------------------------------------------------------------------ self-health

    def repository_size_mb(self) -> float | None:
        row = self.connection.query_one(
            "SELECT SUM(CAST(size AS BIGINT)) * 8 / 1024 AS mb FROM sys.database_files WHERE type_desc = 'ROWS'"
        )
        return float(row["mb"]) if row and row.get("mb") is not None else None


def _split_batches(script: str) -> list[str]:
    """Split a T-SQL script on GO, which is a batch separator for the client, not a statement."""
    batches, current = [], []
    for line in script.splitlines():
        if line.strip().upper() == "GO":
            batch = "\n".join(current).strip()
            if batch:
                batches.append(batch)
            current = []
        else:
            current.append(line)
    tail = "\n".join(current).strip()
    if tail:
        batches.append(tail)
    return batches


def _to_tuples(frame: pd.DataFrame) -> list[tuple]:
    """DataFrame to parameter tuples, with pandas' NA flavours normalized to None for ODBC."""
    prepared = frame.astype(object).where(pd.notna(frame), None)
    rows = []
    for record in prepared.itertuples(index=False, name=None):
        rows.append(tuple(_coerce(value) for value in record))
    return rows


def _coerce(value):
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    if isinstance(value, (pd.Timedelta, timedelta)):
        return str(value)
    # numpy scalars carry through pyodbc badly; unwrap to their Python equivalents.
    item = getattr(value, "item", None)
    if callable(item) and value.__class__.__module__ == "numpy":
        return item()
    return value
