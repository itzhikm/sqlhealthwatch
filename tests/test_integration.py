"""Integration tests against a live SQL Server.

Deselected by default (``addopts = -m 'not integration'``). To run them:

    set SHW_TEST_CONFIG=config          # a config dir pointing at a dev instance
    pytest -m integration

The most valuable test here is :class:`TestQueryMatrix`, which parses every query -- primary *and*
legacy variant -- against the real instance. Parsing is not execution, so it is safe to point at any
instance, and it is the check that catches a query that only compiles on a newer version than the
fleet's floor. Run it once per major version present in the fleet (2008 R2 / 2012 / 2014 / 2016 /
2017 / 2019 / 2022), ideally from throwaway containers.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sqlhealthwatch.config import load_config
from sqlhealthwatch.connection import connect
from sqlhealthwatch.storage.repository import Repository

pytestmark = pytest.mark.integration

CONFIG_DIR = os.environ.get("SHW_TEST_CONFIG")


@pytest.fixture(scope="module")
def config():
    if not CONFIG_DIR:
        pytest.skip("set SHW_TEST_CONFIG to a config directory pointing at a dev instance")
    path = Path(CONFIG_DIR)
    return load_config(path, path.parent.resolve())


@pytest.fixture(scope="module")
def target(config):
    """The first enabled server in the inventory."""
    servers = config.inventory.enabled
    if not servers:
        pytest.skip("no enabled servers in the test inventory")
    return servers[0]


class TestConnectivity:
    def test_every_enabled_server_answers(self, config):
        from sqlhealthwatch.runner import test_connections

        failures = [entry for entry in test_connections(config) if not entry["ok"]]
        assert not failures, f"unreachable: {[(f['server'], f['error']) for f in failures]}"

    def test_the_monitoring_login_has_view_server_state(self, target):
        with connect(target) as connection:
            rows = connection.query("SELECT TOP (1) wait_type FROM sys.dm_os_wait_stats")
            assert rows, "VIEW SERVER STATE appears to be missing"

    def test_feature_probe_reports_a_version(self, config, target):
        from sqlhealthwatch.runner import _probe

        with connect(target) as connection:
            features = _probe(connection, config, target)
        assert features.major_version and features.product_version
        assert features.databases, "the monitoring login cannot enumerate databases"


class TestQueryMatrix:
    """Parse every query against the instance without executing it.

    SET PARSEONLY works on every supported version, so this is the one check that can be pointed at
    a 2008 R2 box and a 2022 box alike.
    """

    def _sql_files(self, config):
        return sorted(p for p in config.sql_dir.glob("*.sql"))

    def test_every_query_parses(self, config, target):
        from sqlhealthwatch.collectors.plan_cache import RANK_ORDER_BY
        from sqlhealthwatch.version import databases_sql

        with connect(target) as connection:
            from sqlhealthwatch.runner import _probe

            features = _probe(connection, config, target)

            failures = []
            for path in self._sql_files(config):
                sql = path.read_text(encoding="utf-8")
                # Fill the placeholders the collectors substitute at runtime.
                sql = sql.replace("{order_by}", next(iter(RANK_ORDER_BY.values())))
                if "{query_store_column}" in sql:
                    sql = databases_sql(features, sql)
                sql = sql.replace("?", "NULL")

                try:
                    connection.execute(f"SET PARSEONLY ON;\n{sql}\nSET PARSEONLY OFF;")
                except Exception as exc:
                    failures.append((path.name, str(exc)[:200]))

            assert not failures, f"queries failed to parse on {features.version_name}: {failures}"

    def test_the_chosen_variants_actually_execute(self, config, target):
        """Run only the variants this instance would really use, and only the cheap ones."""
        from sqlhealthwatch.collectors import ALL_COLLECTORS
        from sqlhealthwatch.collectors.base import ServerContext, load_sql
        from sqlhealthwatch.runner import _probe
        from sqlhealthwatch.util.timeutil import utcnow

        with connect(target) as connection:
            features = _probe(connection, config, target)
            ctx = ServerContext(
                server=target, features=features, connection=connection, settings=config.settings,
                sql_dir=config.sql_dir, run_id="integration", server_id=0,
                collected_at_utc=utcnow(), tier="fast",
            )

            for collector in ALL_COLLECTORS:
                if collector.tier != "fast" or not collector.applies_to(ctx):
                    continue
                filename = collector.sql_file_for(ctx)
                if not filename:
                    continue
                connection.query(load_sql(ctx.sql_dir, filename))


class TestRepositoryRoundTrip:
    def test_schema_bootstraps_and_is_idempotent(self, config):
        with Repository(config.settings.repository, config.settings) as repo:
            repo.bootstrap()
            assert repo.schema_exists()
            repo.bootstrap()  # re-applying must not fail

    def test_write_and_read_back(self, config):
        import pandas as pd

        from sqlhealthwatch.util.timeutil import utcnow

        with Repository(config.settings.repository, config.settings) as repo:
            repo.bootstrap()
            server_id = repo.ensure_server("__integration_test__", "localhost", ["test"])
            run_id = repo.start_run("fast")

            frame = pd.DataFrame([{
                "run_id": run_id, "server_id": server_id, "collected_at_utc": utcnow(),
                "sql_cpu_pct": 42, "other_process_pct": 5, "system_idle_pct": 53,
                "signal_wait_pct": 12.5, "runnable_tasks": 3,
            }])
            assert repo.write("cpu_sample", frame) == 1

            values = repo.recent_values(server_id, "cpu_sample", "sql_cpu_pct", 1)
            assert values == [42]
            repo.finish_run(run_id, 1, 0)

    def test_watermark_round_trip(self, config):
        from datetime import datetime

        with Repository(config.settings.repository, config.settings) as repo:
            server_id = repo.ensure_server("__integration_test__")
            marker = datetime(2026, 8, 30, 2, 15)
            repo.set_watermark(server_id, "deadlocks", marker)
            assert repo.get_watermark(server_id, "deadlocks") == marker

    def test_the_tier_lock_excludes_a_second_holder(self, config):
        with Repository(config.settings.repository, config.settings) as first:
            with first.tier_lock("fast") as acquired_first:
                assert acquired_first
                with Repository(config.settings.repository, config.settings) as second:
                    with second.tier_lock("fast") as acquired_second:
                        # A second run of the same tier must not start.
                        assert not acquired_second


class TestFullRun:
    def test_one_fast_run_completes(self, config):
        from sqlhealthwatch.runner import run_tier

        result = run_tier(config, "fast")
        assert not result.skipped
        assert result.ok_count >= 1, [r.error for r in result.results if not r.ok]

    def test_prune_is_idempotent(self, config):
        from sqlhealthwatch.storage import retention

        with Repository(config.settings.repository, config.settings) as repo:
            retention.prune(repo, config.settings.retention)
            second = retention.prune(repo, config.settings.retention)
            assert not second.errors
