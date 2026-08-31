"""Runner orchestration: a full fast-tier pass, failure isolation, and the overlap guard.

The point of these is the contract the runner promises operationally -- one bad server never costs
the other 39 their collection, and a slow run is skipped rather than piled onto.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime

import pandas as pd
import pytest

from sqlhealthwatch import runner
from sqlhealthwatch.config import load_config
from sqlhealthwatch.connection import ConnectionError_

PROBE_ROW = {
    "product_version": "15.0.4223.1", "major_version": 15, "minor_version": 0,
    "product_level": "RTM", "edition": "Enterprise Edition", "engine_edition": 3,
    "machine_name": "PRDSQL01", "has_stats_properties": 1, "has_volume_stats": 1,
    "has_query_store_objects": 1, "has_extended_events": 1,
}

DATABASE_ROWS = [
    {"database_name": "ERP", "database_id": 5, "state_desc": "ONLINE",
     "recovery_model_desc": "FULL", "compatibility_level": 150, "is_read_only": 0,
     "is_auto_update_stats_on": 1, "is_auto_update_stats_async_on": 0,
     "is_auto_create_stats_on": 1, "is_query_store_on": 1},
]


class FakeConnection:
    """Answers each DMV query with a plausible result set, keyed on a distinctive fragment."""

    def __init__(self) -> None:
        self.closed = False

    def query(self, sql, params=None, timeout_s=None):
        if "ProductVersion" in sql:
            return [PROBE_ROW]
        if "sys.databases" in sql:
            return DATABASE_ROWS
        if "RING_BUFFER_SCHEDULER_MONITOR" in sql:
            return [{"event_time": datetime(2026, 8, 30, 6, 0), "sql_cpu_pct": 88,
                     "system_idle_pct": 7, "other_process_pct": 5}]
        if "signal_wait_time_ms) * 100.0" in sql:
            return [{"signal_wait_pct": 33.0, "total_wait_ms": 900000}]
        if "dm_os_schedulers" in sql:
            return [{"online_schedulers": 16, "runnable_tasks_now": 6, "current_tasks_now": 40,
                     "pending_disk_io": 0}]
        if "dm_os_performance_counters" in sql:
            return [
                {"counter_name": "Page life expectancy", "instance_name": "", "cntr_value": 900},
                {"counter_name": "Memory Grants Pending", "instance_name": "", "cntr_value": 0},
                {"counter_name": "Target Server Memory (KB)", "instance_name": "",
                 "cntr_value": 64 * 1024 * 1024},
                {"counter_name": "Total Server Memory (KB)", "instance_name": "",
                 "cntr_value": 60 * 1024 * 1024},
            ]
        if "dm_io_virtual_file_stats" in sql:
            return [{"database_name": "ERP", "file_type": "ROWS", "physical_name": "E:\\erp.mdf",
                     "num_of_reads": 5000, "num_of_writes": 900,
                     "num_of_bytes_read": 10 ** 9, "num_of_bytes_written": 10 ** 8,
                     "io_stall_read_ms": 60000, "io_stall_write_ms": 4000, "io_stall": 64000,
                     "avg_read_latency_ms": 12.0, "avg_write_latency_ms": 4.4}]
        if "dm_os_wait_stats" in sql:
            return [{"wait_type": "PAGEIOLATCH_SH", "wait_time_ms": 500000,
                     "waiting_tasks_count": 900, "resource_wait_ms": 480000,
                     "signal_wait_time_ms": 20000}]
        if "dm_os_volume_stats" in sql:
            return [{"volume_mount_point": "E:\\", "total_gb": 1000, "free_gb": 60, "free_pct": 6.0}]
        if "blocking_session_id" in sql:
            return []
        return []

    def query_one(self, sql, params=None, timeout_s=None):
        rows = self.query(sql, params, timeout_s)
        return rows[0] if rows else None

    def query_in_database(self, database, sql, params=None, timeout_s=None):
        return self.query(sql, params, timeout_s)

    def close(self):
        self.closed = True


class FakeRepository:
    """Records what the runner wrote, so the assertions can be about behaviour, not SQL."""

    instances: list["FakeRepository"] = []

    def __init__(self, config=None, settings=None, lock_available=True) -> None:
        self.writes: dict[str, pd.DataFrame] = {}
        self.findings: list = []
        self.statuses: list[tuple] = []
        self.alerts: list[tuple] = []
        self.watermarks: dict[tuple, datetime] = {}
        self.lock_available = lock_available
        self.run_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        FakeRepository.instances.append(self)

    # lifecycle -------------------------------------------------------------------------------
    def connect(self):
        return self

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def table(self, name):
        return f"[mon].[{name}]"

    # bootstrap / locking ---------------------------------------------------------------------
    def schema_exists(self):
        return True

    def bootstrap(self):
        pass

    @contextmanager
    def tier_lock(self, tier):
        yield self.lock_available

    # run bookkeeping -------------------------------------------------------------------------
    def start_run(self, tier):
        return self.run_id

    def finish_run(self, run_id, ok, failed, notes=None):
        self.finished = (run_id, ok, failed)

    def ensure_server(self, name, host=None, tags=()):
        return {"PRD-SQL-01": 1, "PRD-SQL-02": 2, "PRD-DBA-REPO": 3}.get(name, 9)

    def record_server_status(self, run_id, server_id, ok, duration_ms, error=None):
        self.statuses.append((server_id, ok, error))

    def update_server_features(self, server_id, features):
        self.features = features

    # writes ----------------------------------------------------------------------------------
    def write(self, table, frame):
        self.writes[table] = frame
        return len(frame)

    def write_findings(self, findings):
        self.findings.extend(findings)
        return len(list(findings))

    def log_alert(self, server_id, fingerprint, severity, channel, ok, error=None):
        self.alerts.append((server_id, fingerprint, channel, ok))

    # reads -----------------------------------------------------------------------------------
    @property
    def connection(self):
        return _RunIdConnection(self.run_id)

    def get_watermark(self, server_id, collector):
        return self.watermarks.get((server_id, collector))

    def set_watermark(self, server_id, collector, value):
        self.watermarks[(server_id, collector)] = value

    def recent_values(self, server_id, table, column, limit=8):
        if table == "cpu_sample" and column == "sql_cpu_pct":
            return [85.0, 87.0, 89.0, 88.0]
        return []

    def previous_io_sample(self, server_id):
        return pd.DataFrame()

    def growth_series(self, server_id, days=7):
        return pd.DataFrame()

    def drive_growth_series(self, server_id, days=7):
        return pd.DataFrame()

    def deadlock_count(self, server_id, hours=24):
        return 0

    def last_alert_time(self, fingerprint):
        return None


class _RunIdConnection:
    def __init__(self, run_id):
        self.run_id = run_id

    def query_one(self, sql, params=None, timeout_s=None):
        return {"run_id": self.run_id}


@pytest.fixture
def config(project_root):
    return load_config(project_root / "config", project_root)


@pytest.fixture(autouse=True)
def clean_instances():
    FakeRepository.instances = []
    yield
    FakeRepository.instances = []


@pytest.fixture
def patched(monkeypatch):
    def _apply(connect_impl=None, lock_available=True):
        monkeypatch.setattr(
            runner, "Repository",
            lambda *args, **kwargs: FakeRepository(lock_available=lock_available),
        )
        monkeypatch.setattr(
            runner, "connect",
            connect_impl or (lambda server, retries=1: FakeConnection()),
        )
        # No alerts leave the building in tests; the router itself is covered separately.
        monkeypatch.setattr(runner.AlertRouter, "dispatch",
                            lambda self, findings, server, server_id=None, dry_run=False: None)
    return _apply


class TestSuccessfulRun:
    def test_a_fast_run_collects_stores_and_analyzes(self, config, patched):
        patched()
        result = runner.run_tier(config, "fast", only=["PRD-SQL-01"])

        assert result.ok_count == 1 and result.failed_count == 0
        entry = result.results[0]
        assert entry.ok and entry.total_rows > 0

    def test_every_fast_collector_writes_its_table(self, config, patched):
        patched()
        runner.run_tier(config, "fast", only=["PRD-SQL-01"])

        written = {table for repo in FakeRepository.instances for table in repo.writes}
        assert {"cpu_sample", "memory_sample", "io_file_sample", "wait_sample",
                "space_drive_sample"} <= written

    def test_rows_carry_run_server_and_time_identity(self, config, patched):
        patched()
        runner.run_tier(config, "fast", only=["PRD-SQL-01"])

        frame = next(repo.writes["cpu_sample"] for repo in FakeRepository.instances
                     if "cpu_sample" in repo.writes)
        assert list(frame.columns[:3]) == ["run_id", "server_id", "collected_at_utc"]
        assert frame.iloc[0]["server_id"] == 1

    def test_thresholds_produce_findings(self, config, patched):
        patched()
        result = runner.run_tier(config, "fast", only=["PRD-SQL-01"])

        metrics = {f.metric for f in result.findings}
        # CPU has averaged 87% over four samples, and PLE is far below the scaled floor.
        assert "sql_cpu_pct" in metrics
        assert "page_life_expectancy" in metrics

    def test_features_are_cached_on_the_dimension_row(self, config, patched):
        patched()
        runner.run_tier(config, "fast", only=["PRD-SQL-01"])

        repo = next(r for r in FakeRepository.instances if hasattr(r, "features"))
        assert repo.features.supports_query_store is True

    def test_dry_run_writes_nothing(self, config, patched):
        patched()
        result = runner.run_tier(config, "fast", only=["PRD-SQL-01"], dry_run=True)

        assert result.ok_count == 1
        assert all(not repo.writes for repo in FakeRepository.instances)


class TestFailureIsolation:
    def test_an_unreachable_server_does_not_abort_the_run(self, config, patched):
        def flaky_connect(server, retries=1):
            if server.name == "PRD-SQL-02":
                raise ConnectionError_("prd-sql-02.corp.local,1433: Login timeout expired")
            return FakeConnection()

        patched(connect_impl=flaky_connect)
        result = runner.run_tier(config, "fast")

        # The other servers still completed.
        assert result.ok_count >= 1
        assert result.failed_count == 1
        failure = next(r for r in result.results if not r.ok)
        assert "Login timeout expired" in failure.error

    def test_an_unreachable_server_raises_a_critical_availability_finding(self, config, patched):
        def flaky_connect(server, retries=1):
            if server.name == "PRD-SQL-02":
                raise ConnectionError_("Login timeout expired")
            return FakeConnection()

        patched(connect_impl=flaky_connect)
        result = runner.run_tier(config, "fast")

        availability = [f for f in result.findings if f.category == "availability"]
        assert availability and availability[0].severity == "crit"
        assert availability[0].server_name == "PRD-SQL-02"

    def test_the_failure_is_recorded_against_the_server(self, config, patched):
        def flaky_connect(server, retries=1):
            if server.name == "PRD-SQL-02":
                raise ConnectionError_("Login timeout expired")
            return FakeConnection()

        patched(connect_impl=flaky_connect)
        runner.run_tier(config, "fast")

        statuses = [s for repo in FakeRepository.instances for s in repo.statuses]
        assert any(server_id == 2 and ok is False for server_id, ok, _ in statuses)

    def test_one_failing_collector_does_not_cost_the_server_its_other_metrics(self, config, patched,
                                                                             monkeypatch):
        class PartlyBrokenConnection(FakeConnection):
            def query(self, sql, params=None, timeout_s=None):
                if "dm_os_performance_counters" in sql:
                    raise RuntimeError("VIEW SERVER STATE denied")
                return super().query(sql, params, timeout_s)

        patched(connect_impl=lambda server, retries=1: PartlyBrokenConnection())
        result = runner.run_tier(config, "fast", only=["PRD-SQL-01"])

        entry = result.results[0]
        assert entry.ok
        assert "memory" in entry.skipped_collectors
        written = {table for repo in FakeRepository.instances for table in repo.writes}
        assert "cpu_sample" in written and "memory_sample" not in written

    def test_the_connection_is_always_closed(self, config, patched):
        opened = []

        def tracking_connect(server, retries=1):
            connection = FakeConnection()
            opened.append(connection)
            return connection

        patched(connect_impl=tracking_connect)
        runner.run_tier(config, "fast", only=["PRD-SQL-01"])

        assert opened and all(connection.closed for connection in opened)


class TestOverlapGuard:
    def test_a_run_is_skipped_while_the_previous_one_is_still_going(self, config, patched):
        patched(lock_available=False)
        result = runner.run_tier(config, "fast")

        assert result.skipped
        assert "still running" in result.skip_reason
        # Nothing was collected, so nothing was written.
        assert all(not repo.writes for repo in FakeRepository.instances)


class TestSelection:
    def test_disabled_servers_are_not_collected(self, config, patched):
        patched()
        config.inventory.servers[0].enabled = False
        result = runner.run_tier(config, "fast")

        assert all(entry.server.name != "PRD-SQL-01" for entry in result.results)

    def test_an_unknown_server_name_is_an_error(self, config, patched):
        patched()
        with pytest.raises(ValueError, match="no enabled servers matched"):
            runner.run_tier(config, "fast", only=["NOPE"])


class TestCollectorPlan:
    def test_tiers_are_separated(self, config):
        fast = {c.name for c in runner.collector_plan(config, "fast")}
        daily = {c.name for c in runner.collector_plan(config, "daily")}

        assert "cpu" in fast and "index_frag" not in fast
        assert "index_frag" in daily and "cpu" not in daily
        assert not fast & daily
