"""Collector transforms, driven by captured DMV result sets -- no database involved.

These are the tests that catch a version-gating mistake: the same collector is run against a modern
feature set and a legacy one, and the assertion is on which SQL variant it chose and what it did
with the reduced data.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from sqlhealthwatch.collectors.blocking import BlockingCollector, chain_depth
from sqlhealthwatch.collectors.cpu import CpuCollector
from sqlhealthwatch.collectors.deadlocks import DeadlockCollector, parse_deadlock_graph
from sqlhealthwatch.collectors.indexes import IndexFragCollector
from sqlhealthwatch.collectors.memory import MemoryCollector
from sqlhealthwatch.collectors.space import SpaceDriveCollector
from sqlhealthwatch.collectors.statistics import StatisticsCollector
from sqlhealthwatch.version import ServerFeatures


class TestCpuCollector:
    def test_ring_buffer_sample_becomes_a_row(self, make_context, modern_features, fake_connection):
        connection = fake_connection(
            [
                [{"event_time": datetime(2026, 8, 30, 6, 0), "sql_cpu_pct": 82,
                  "system_idle_pct": 10, "other_process_pct": 8}],
                [{"signal_wait_pct": 31.5, "total_wait_ms": 900000}],
                [{"runnable_tasks_now": 7, "online_schedulers": 16}],
            ]
        )
        ctx = make_context(modern_features, connection)
        frame = CpuCollector().collect(ctx)

        row = frame.iloc[0]
        assert row["sql_cpu_pct"] == 82
        assert row["signal_wait_pct"] == 31.5
        assert row["runnable_tasks"] == 7
        # Identity columns are stamped on every collected row.
        assert row["server_id"] == 1 and row["run_id"].startswith("11111111")

    def test_2005_records_null_cpu_rather_than_guessing(self, make_context, fake_connection):
        features = ServerFeatures(major_version=9, minor_version=0, engine_edition=3)
        connection = fake_connection([[{"signal_wait_pct": 12.0}], [{"runnable_tasks_now": 1}]])
        ctx = make_context(features, connection)

        frame = CpuCollector().collect(ctx)

        assert frame.iloc[0]["sql_cpu_pct"] is None
        assert any("CPU %" in note for note in ctx.notes)

    def test_skipped_on_azure_sql_db(self, make_context):
        features = ServerFeatures(major_version=12, engine_edition=5)
        assert CpuCollector().applies_to(make_context(features)) is False


class TestMemoryCollector:
    def _counters(self):
        return [
            {"counter_name": "Page life expectancy", "instance_name": "", "cntr_value": 2400},
            {"counter_name": "Page life expectancy", "instance_name": "node0", "cntr_value": 4000},
            {"counter_name": "Page life expectancy", "instance_name": "node1", "cntr_value": 300},
            {"counter_name": "Buffer cache hit ratio", "instance_name": "", "cntr_value": 990},
            {"counter_name": "Buffer cache hit ratio base", "instance_name": "", "cntr_value": 1000},
            {"counter_name": "Memory Grants Pending", "instance_name": "", "cntr_value": 3},
            {"counter_name": "Total Server Memory (KB)", "instance_name": "", "cntr_value": 60 * 1024 * 1024},
            {"counter_name": "Target Server Memory (KB)", "instance_name": "", "cntr_value": 64 * 1024 * 1024},
        ]

    def test_counters_are_normalized_into_one_row(self, make_context, modern_features):
        ctx = make_context(modern_features)
        frame = MemoryCollector().transform(self._counters(), ctx)
        row = frame.iloc[0]

        assert row["page_life_expectancy"] == 2400
        assert row["memory_grants_pending"] == 3
        assert row["target_server_memory_mb"] == 64 * 1024

    def test_cache_hit_ratio_is_divided_by_its_base(self, make_context, modern_features):
        # The raw counter is a ratio pair; 990/1000 is 99%, not 990%.
        frame = MemoryCollector().transform(self._counters(), make_context(modern_features))
        assert frame.iloc[0]["buffer_cache_hit_ratio"] == 99.0

    def test_worst_numa_node_is_kept_separately(self, make_context, modern_features):
        # A starved node hides inside a healthy instance-level figure.
        frame = MemoryCollector().transform(self._counters(), make_context(modern_features))
        assert frame.iloc[0]["min_node_ple"] == 300

    def test_ple_floor_scales_with_the_buffer_pool(self, make_context, modern_features):
        frame = MemoryCollector().transform(self._counters(), make_context(modern_features))
        assert frame.iloc[0]["ple_dynamic_floor"] == 4800

    def test_legacy_instance_uses_the_variant_without_buffer_node(self, make_context, legacy_features):
        # 2008 R2 has the Buffer Node object; a pre-2008 box does not.
        old = ServerFeatures(major_version=9, minor_version=0, engine_edition=3)
        assert MemoryCollector().sql_file_for(make_context(old)) == "perf_counters_legacy.sql"
        assert MemoryCollector().sql_file_for(make_context(legacy_features)) == "perf_counters.sql"


class TestSpaceDriveCollector:
    def test_modern_path_keeps_percentages(self, make_context, modern_features):
        ctx = make_context(modern_features)
        assert SpaceDriveCollector().sql_file_for(ctx) == "space_drive.sql"

        rows = [{"volume_mount_point": "E:\\", "total_gb": 1000, "free_gb": 120, "free_pct": 12.0}]
        frame = SpaceDriveCollector().transform(rows, ctx)
        assert frame.iloc[0]["free_pct"] == 12.0

    def test_legacy_path_leaves_total_and_percent_null(self, make_context, legacy_features):
        ctx = make_context(legacy_features)
        assert SpaceDriveCollector().sql_file_for(ctx) == "space_drive_legacy.sql"
        assert any("xp_fixeddrives" in note for note in ctx.notes)

        rows = [{"volume_mount_point": "E", "total_gb": None, "free_gb": 12.5, "free_pct": None}]
        frame = SpaceDriveCollector().transform(rows, ctx)
        row = frame.iloc[0]

        # No denominator exists on this path, so no percentage is invented.
        assert row["free_gb"] == 12.5
        assert row["total_gb"] is None and row["free_pct"] is None


class TestStatisticsCollector:
    def test_modern_path_is_not_flagged_as_an_estimate(self, make_context, modern_features):
        ctx = make_context(modern_features)
        assert StatisticsCollector().sql_file_for(ctx) == "stats_age.sql"

        rows = [{"database_name": "ERP", "schema_name": "dbo", "table_name": "Orders",
                 "stats_name": "IX_Orders", "last_updated": datetime(2026, 8, 1),
                 "rows": 500000, "modification_counter": 150000, "modification_ratio": 0.3,
                 "days_since_update": 29, "no_recompute": False}]
        frame = StatisticsCollector().transform(rows, ctx)
        assert bool(frame.iloc[0]["is_estimate"]) is False

    def test_legacy_path_marks_rows_as_estimates(self, make_context, legacy_features):
        ctx = make_context(legacy_features)
        assert StatisticsCollector().sql_file_for(ctx) == "stats_age_legacy.sql"
        assert any("rowmodctr" in note for note in ctx.notes)

        rows = [{"database_name": "LEGACY", "schema_name": "dbo", "table_name": "Orders",
                 "stats_name": "IX_Orders", "last_updated": datetime(2026, 8, 1),
                 "rows": 500000, "modification_counter": 150000, "modification_ratio": 0.3,
                 "days_since_update": 29, "no_recompute": False}]
        frame = StatisticsCollector().transform(rows, ctx)
        assert bool(frame.iloc[0]["is_estimate"]) is True

    def test_read_only_databases_are_skipped(self, make_context, modern_features):
        modern_features.databases[0].is_read_only = True
        ctx = make_context(modern_features)
        names = [db.name for db in StatisticsCollector().databases(ctx)]
        # Statistics on a read-only database cannot be updated, so a finding there is noise.
        assert "ERP" not in names and "Archive" in names


class TestBlockingCollector:
    def test_chain_depth_is_computed_from_the_pairs(self):
        # 100 blocked by 101, 101 blocked by 102 -- 100 is three deep.
        pairs = {100: 101, 101: 102, 102: None}
        assert chain_depth(102, pairs) == 1
        assert chain_depth(101, pairs) == 2
        assert chain_depth(100, pairs) == 3

    def test_a_cycle_does_not_loop_forever(self):
        assert chain_depth(1, {1: 2, 2: 1}) >= 1

    def test_transform_records_depth(self, make_context, modern_features):
        rows = [
            {"blocked_spid": 100, "blocking_spid": 101, "wait_type": "LCK_M_S",
             "wait_seconds": 45.0, "database_name": "ERP", "blocked_stmt": "SELECT 1"},
            {"blocked_spid": 101, "blocking_spid": 102, "wait_type": "LCK_M_X",
             "wait_seconds": 60.0, "database_name": "ERP", "blocked_stmt": "UPDATE t SET x = 1"},
        ]
        frame = BlockingCollector().transform(rows, make_context(modern_features))
        assert list(frame["chain_depth"]) == [3, 2]


class TestDeadlockCollector:
    GRAPH = """
    <deadlock>
      <victim-list><victimProcess id="process1a2b"/></victim-list>
      <process-list>
        <process id="process1a2b" spid="57" currentdb="5">
          <executionStack><frame>UPDATE dbo.Orders SET Total = 1</frame></executionStack>
          <inputbuf>exec usp_PlaceOrder</inputbuf>
        </process>
        <process id="process3c4d" spid="61" currentdb="5"><inputbuf>exec usp_Ship</inputbuf></process>
      </process-list>
      <resource-list>
        <keylock objectname="ERP.dbo.Orders" indexname="PK_Orders"/>
        <keylock objectname="ERP.dbo.OrderLines" indexname="PK_OrderLines"/>
      </resource-list>
    </deadlock>
    """

    def test_graph_is_parsed_into_indexable_columns(self):
        parsed = parse_deadlock_graph(self.GRAPH)
        assert parsed["victim_spid"] == 57
        assert parsed["participant_count"] == 2
        assert parsed["database_name"] == "ERP"
        assert "ERP.dbo.Orders" in parsed["objects"]
        assert "UPDATE dbo.Orders" in parsed["victim_statement"]

    def test_unparseable_graph_still_stores_the_raw_xml(self, make_context, modern_features):
        rows = [{"deadlock_time_utc": datetime(2026, 8, 30, 2, 15), "deadlock_graph": "<not-xml"}]
        frame = DeadlockCollector().transform(rows, make_context(modern_features))
        row = frame.iloc[0]

        # The parsed columns are an index into the graph; the graph itself is the durable artifact.
        assert row["victim_spid"] is None
        assert row["deadlock_graph"] == "<not-xml"
        assert row["dedup_key"]

    def test_dedup_key_is_stable_and_distinguishes_events(self, make_context, modern_features):
        ctx = make_context(modern_features)
        first = DeadlockCollector().transform(
            [{"deadlock_time_utc": datetime(2026, 8, 30, 2, 15), "deadlock_graph": self.GRAPH}], ctx
        )
        again = DeadlockCollector().transform(
            [{"deadlock_time_utc": datetime(2026, 8, 30, 2, 15), "deadlock_graph": self.GRAPH}], ctx
        )
        later = DeadlockCollector().transform(
            [{"deadlock_time_utc": datetime(2026, 8, 30, 3, 30), "deadlock_graph": self.GRAPH}], ctx
        )

        assert first.iloc[0]["dedup_key"] == again.iloc[0]["dedup_key"]
        assert first.iloc[0]["dedup_key"] != later.iloc[0]["dedup_key"]

    def test_repeated_events_in_one_batch_are_collapsed(self, make_context, modern_features):
        rows = [{"deadlock_time_utc": datetime(2026, 8, 30, 2, 15), "deadlock_graph": self.GRAPH}] * 3
        frame = DeadlockCollector().transform(rows, make_context(modern_features))
        assert len(frame) == 1

    def test_target_choice_follows_the_version(self, make_context, modern_features, legacy_features):
        assert DeadlockCollector().sql_file_for(make_context(modern_features)) == "deadlocks.sql"
        # 2008 R2's ring buffer layout differs and its file target is usually unreadable.
        assert DeadlockCollector().sql_file_for(make_context(legacy_features)) == "deadlocks_2008.sql"

    def test_skipped_without_extended_events(self, make_context):
        features = ServerFeatures(major_version=9, engine_edition=3, has_extended_events=False)
        ctx = make_context(features)
        assert DeadlockCollector().applies_to(ctx) is False
        assert any("Extended Events" in note for note in ctx.notes)

    def test_watermark_is_the_newest_event(self, make_context, modern_features):
        frame = pd.DataFrame(
            [
                {"deadlock_time_utc": datetime(2026, 8, 30, 2, 15)},
                {"deadlock_time_utc": datetime(2026, 8, 30, 5, 45)},
            ]
        )
        assert DeadlockCollector().watermark_from(frame) == datetime(2026, 8, 30, 5, 45)


class TestIndexFragCollector:
    def test_rows_are_normalized(self, make_context, modern_features):
        rows = [{"database_name": "ERP", "schema_name": "dbo", "table_name": "Orders",
                 "index_name": "IX_Orders_Date", "index_type": "NONCLUSTERED INDEX",
                 "avg_fragmentation_pct": 42.7, "page_count": 12000, "recommendation": "REBUILD"}]
        frame = IndexFragCollector().transform(rows, make_context(modern_features))
        row = frame.iloc[0]
        assert row["avg_fragmentation_pct"] == 42.7
        assert row["recommendation"] == "REBUILD"
