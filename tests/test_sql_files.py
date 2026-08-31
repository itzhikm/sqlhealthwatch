"""Every SQL file a collector can ask for must exist, and the version variants must come in pairs.

This is the cheap half of the query-validation story. The other half -- running each query against a
real instance of each major version in the fleet -- is the integration suite, which needs a database
and is deselected by default.
"""

from __future__ import annotations

import re

import pytest

from sqlhealthwatch.collectors import ALL_COLLECTORS
from sqlhealthwatch.collectors.plan_cache import RANK_ORDER_BY
from sqlhealthwatch.collectors.query_store import QS_ORDER_BY

# (primary, legacy) pairs -- the legacy file is what a pre-min-version instance falls back to.
VARIANT_PAIRS = [
    ("space_drive.sql", "space_drive_legacy.sql"),
    ("stats_age.sql", "stats_age_legacy.sql"),
    ("perf_counters.sql", "perf_counters_legacy.sql"),
    ("memory_clerks.sql", "memory_clerks_legacy.sql"),
    ("instance_meta.sql", "instance_meta_legacy.sql"),
    ("plan_cache_top.sql", "plan_cache_top_legacy.sql"),
    ("deadlocks.sql", "deadlocks_2008.sql"),
]


@pytest.fixture
def sql_dir(project_root):
    return project_root / "sql"


class TestFilesExist:
    @pytest.mark.parametrize("primary,legacy", VARIANT_PAIRS)
    def test_both_variants_are_present(self, sql_dir, primary, legacy):
        assert (sql_dir / primary).exists(), f"missing {primary}"
        assert (sql_dir / legacy).exists(), f"missing {legacy}"

    def test_every_collector_sql_file_exists(self, sql_dir, make_context, modern_features,
                                             legacy_features):
        # Both feature sets are exercised so a legacy-only variant cannot go missing unnoticed.
        for features in (modern_features, legacy_features):
            ctx = make_context(features)
            for collector in ALL_COLLECTORS:
                filename = collector.sql_file_for(ctx)
                if filename:
                    assert (sql_dir / filename).exists(), f"{collector.name} -> missing {filename}"

    def test_repository_provisioning_scripts_are_present(self, sql_dir):
        assert (sql_dir / "repository" / "create_database.sql").exists()
        assert (sql_dir / "repository" / "create_repo_login.sql").exists()
        assert (sql_dir / "repository" / "create_monitor_login.sql").exists()


class TestQueryShape:
    def test_no_query_is_empty(self, sql_dir):
        for path in sql_dir.rglob("*.sql"):
            body = "\n".join(
                line for line in path.read_text(encoding="utf-8").splitlines()
                if not line.strip().startswith("--")
            )
            assert body.strip(), f"{path.name} has no statements"

    def test_fragmentation_never_uses_a_detailed_scan(self, sql_dir):
        # DETAILED reads every page; it must never run against production.
        text = (sql_dir / "index_frag.sql").read_text(encoding="utf-8").upper()
        assert "'LIMITED'" in text
        assert "'DETAILED'" not in text

    def test_no_query_writes_to_a_monitored_instance(self, sql_dir):
        # The only exception is the temp table xp_fixeddrives needs to capture its output.
        forbidden = re.compile(r"\b(UPDATE|DELETE|TRUNCATE|ALTER|MERGE)\b", re.IGNORECASE)
        for path in sql_dir.glob("*.sql"):
            body = "\n".join(
                line for line in path.read_text(encoding="utf-8").splitlines()
                if not line.strip().startswith("--")
            )
            assert not forbidden.search(body), f"{path.name} is not read-only"

    def test_the_legacy_drive_query_only_touches_a_temp_table(self, sql_dir):
        text = (sql_dir / "space_drive_legacy.sql").read_text(encoding="utf-8")
        assert "#fixeddrives" in text
        assert "xp_fixeddrives" in text

    def test_feature_probe_avoids_productmajorversion(self, sql_dir):
        # That property is NULL before 2014 SP2, which is exactly the fleet this has to work on.
        text = (sql_dir / "feature_probe.sql").read_text(encoding="utf-8")
        statements = "\n".join(
            line for line in text.splitlines() if not line.strip().startswith("--")
        )
        assert "ProductMajorVersion" not in statements
        assert "PARSENAME" in statements

    def test_probe_gates_on_object_existence(self, sql_dir):
        text = (sql_dir / "feature_probe.sql").read_text(encoding="utf-8")
        for dmv in ("sys.dm_db_stats_properties", "sys.dm_os_volume_stats",
                    "sys.query_store_query", "sys.dm_xe_sessions"):
            assert f"OBJECT_ID('{dmv}')" in text


class TestPlaceholders:
    def test_query_files_expose_the_order_by_placeholder(self, sql_dir):
        for filename in ("querystore_top.sql", "plan_cache_top.sql", "plan_cache_top_legacy.sql"):
            assert "{order_by}" in (sql_dir / filename).read_text(encoding="utf-8")

    def test_every_rank_substitutes_cleanly(self, sql_dir):
        for filename, mapping in (("querystore_top.sql", QS_ORDER_BY),
                                  ("plan_cache_top.sql", RANK_ORDER_BY)):
            template = (sql_dir / filename).read_text(encoding="utf-8")
            for rank, column in mapping.items():
                rendered = template.replace("{order_by}", column)
                assert "{order_by}" not in rendered
                assert f"ORDER BY {column} DESC" in rendered, f"{filename} / {rank}"

    def test_order_by_columns_are_selected_by_the_query(self, sql_dir):
        # A ranking column that is not in the SELECT list would fail at runtime, on the fleet.
        for filename, mapping in (("querystore_top.sql", QS_ORDER_BY),
                                  ("plan_cache_top.sql", RANK_ORDER_BY)):
            text = (sql_dir / filename).read_text(encoding="utf-8")
            for column in mapping.values():
                assert column in text, f"{filename} does not select {column}"

    def test_databases_query_exposes_the_query_store_placeholder(self, sql_dir):
        assert "{query_store_column}" in (sql_dir / "databases.sql").read_text(encoding="utf-8")
