"""Threshold evaluation: severity selection, the legacy space path, and escalation."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from sqlhealthwatch.analyze.thresholds import AnalysisInput, evaluate, unreachable
from sqlhealthwatch.config import ServerConfig

NOW = datetime(2026, 8, 30, 6, 0)

DEFAULTS = {
    "cpu": {"sustained_pct_warn": 80, "sustained_pct_crit": 90, "sustained_samples": 4,
            "signal_wait_pct_warn": 25},
    "memory": {"ple_warn": 300, "ple_crit": 180, "memory_grants_pending_warn": 1},
    "io": {"read_latency_ms_warn": 20, "read_latency_ms_crit": 50,
           "write_latency_ms_warn": 20, "write_latency_ms_crit": 50},
    "space": {"db_free_pct_warn": 15, "db_free_pct_crit": 8,
              "drive_free_pct_warn": 15, "drive_free_pct_crit": 8,
              "drive_free_mb_warn": 20480, "drive_free_mb_crit": 10240,
              "days_to_full_warn": 7},
    "blocking": {"block_seconds_warn": 30, "block_seconds_crit": 120},
    "deadlock": {"count_24h_warn": 1, "count_24h_crit": 10},
    "index": {"frag_pct_min_report": 15, "min_page_count": 1000},
    "stats": {"stale_days_warn": 7, "modification_ratio_warn": 0.20},
}


def make_input(features, server=None, **kwargs) -> AnalysisInput:
    return AnalysisInput(
        server=server or ServerConfig(name="PRD-SQL-01", host="h"),
        server_id=1,
        run_id="run",
        features=features,
        thresholds=DEFAULTS,
        now=NOW,
        **kwargs,
    )


def find(findings, metric):
    return next((f for f in findings if f.metric == metric), None)


class TestCpuRules:
    def test_sustained_cpu_warns(self, modern_features):
        findings = evaluate(make_input(modern_features, cpu_history=[70, 82, 85, 88]))
        finding = find(findings, "sql_cpu_pct")
        assert finding and finding.severity == "warn"

    def test_sustained_cpu_crits(self, modern_features):
        findings = evaluate(make_input(modern_features, cpu_history=[95, 96, 97, 98]))
        assert find(findings, "sql_cpu_pct").severity == "crit"

    def test_a_single_spike_is_not_sustained(self, modern_features):
        # Three samples where four are required: no finding yet.
        findings = evaluate(make_input(modern_features, cpu_history=[99, 99, 99]))
        assert find(findings, "sql_cpu_pct") is None

    def test_signal_wait_warns(self, modern_features):
        frames = {"cpu_sample": pd.DataFrame([{"signal_wait_pct": 40.0, "sql_cpu_pct": 30}])}
        findings = evaluate(make_input(modern_features, frames=frames))
        assert find(findings, "signal_wait_pct").severity == "warn"


class TestMemoryRules:
    def test_ple_below_the_absolute_floor_is_critical(self, modern_features):
        frames = {"memory_sample": pd.DataFrame(
            [{"page_life_expectancy": 120, "ple_dynamic_floor": 4800, "min_node_ple": None}]
        )}
        assert find(evaluate(make_input(modern_features, frames=frames)),
                    "page_life_expectancy").severity == "crit"

    def test_ple_is_judged_against_the_scaled_floor_not_just_300(self, modern_features):
        # 1200s would pass a flat 300s rule, but this box has a 64 GB buffer pool.
        frames = {"memory_sample": pd.DataFrame(
            [{"page_life_expectancy": 1200, "ple_dynamic_floor": 4800, "min_node_ple": None}]
        )}
        finding = find(evaluate(make_input(modern_features, frames=frames)), "page_life_expectancy")
        assert finding.severity == "warn"
        assert finding.threshold == 4800

    def test_healthy_ple_produces_nothing(self, modern_features):
        frames = {"memory_sample": pd.DataFrame(
            [{"page_life_expectancy": 9000, "ple_dynamic_floor": 4800, "min_node_ple": 8800}]
        )}
        assert find(evaluate(make_input(modern_features, frames=frames)), "page_life_expectancy") is None

    def test_a_starved_numa_node_is_surfaced(self, modern_features):
        frames = {"memory_sample": pd.DataFrame(
            [{"page_life_expectancy": 9000, "ple_dynamic_floor": 300, "min_node_ple": 100}]
        )}
        assert find(evaluate(make_input(modern_features, frames=frames)), "min_node_ple") is not None

    def test_grants_pending_needs_two_consecutive_samples(self, modern_features):
        frames = {"memory_sample": pd.DataFrame([{"page_life_expectancy": 9000}])}
        one_blip = evaluate(make_input(modern_features, frames=frames, grants_history=[0, 3]))
        sustained = evaluate(make_input(modern_features, frames=frames, grants_history=[2, 3]))

        assert find(one_blip, "memory_grants_pending") is None
        assert find(sustained, "memory_grants_pending") is not None


class TestIoRules:
    def _frame(self, read_ms, write_ms=None):
        return {"io_file_sample": pd.DataFrame([{
            "database_name": "ERP", "physical_name": "E:\\erp.mdf", "file_type": "ROWS",
            "interval_read_latency_ms": read_ms, "interval_write_latency_ms": write_ms,
        }])}

    def test_latency_over_the_crit_threshold(self, modern_features):
        findings = evaluate(make_input(modern_features, frames=self._frame(80.0)))
        assert find(findings, "read_latency_ms").severity == "crit"

    def test_no_io_in_the_window_produces_no_finding(self, modern_features):
        # None means "nothing happened", not "zero milliseconds".
        findings = evaluate(make_input(modern_features, frames=self._frame(None)))
        assert find(findings, "read_latency_ms") is None

    def test_the_file_is_named_in_the_fingerprint(self, modern_features):
        finding = find(evaluate(make_input(modern_features, frames=self._frame(80.0))), "read_latency_ms")
        assert "E:\\erp.mdf" in finding.fingerprint


class TestSpaceRules:
    def test_low_database_free_space_crits(self, modern_features):
        frames = {"space_db_sample": pd.DataFrame([{
            "database_name": "ERP", "logical_name": "ERP_Data", "free_pct": 5.0,
            "free_mb": 500, "size_mb": 10000,
        }])}
        assert find(evaluate(make_input(modern_features, frames=frames)), "db_free_pct").severity == "crit"

    def test_modern_drive_path_uses_percentage(self, modern_features):
        frames = {"space_drive_sample": pd.DataFrame(
            [{"volume_mount_point": "E:\\", "total_gb": 1000, "free_gb": 60, "free_pct": 6.0}]
        )}
        findings = evaluate(make_input(modern_features, frames=frames))
        assert find(findings, "drive_free_pct").severity == "crit"
        assert find(findings, "drive_free_mb") is None

    def test_legacy_drive_path_switches_to_absolute_megabytes(self, legacy_features):
        # xp_fixeddrives gives no volume total, so there is no percentage to threshold against.
        frames = {"space_drive_sample": pd.DataFrame(
            [{"volume_mount_point": "E", "total_gb": None, "free_gb": 8.0, "free_pct": None}]
        )}
        findings = evaluate(make_input(legacy_features, frames=frames))
        finding = find(findings, "drive_free_mb")

        assert finding and finding.severity == "crit"
        assert finding.details["legacy_free_mb_only"] is True
        assert "free % unavailable" in finding.message
        assert find(findings, "drive_free_pct") is None

    def test_days_to_full_projection_warns(self, modern_features):
        points = [(NOW - timedelta(days=6 - i), 100.0 + i * 100) for i in range(7)]
        growth = {("ERP", "ERP_Data"): (points, 1400.0)}
        findings = evaluate(make_input(modern_features, db_growth=growth))
        assert find(findings, "db_days_to_full") is not None


class TestBlockingAndDeadlocks:
    def test_longest_block_is_reported(self, modern_features):
        frames = {"blocking_event": pd.DataFrame([
            {"blocked_spid": 100, "blocking_spid": 101, "wait_seconds": 45.0,
             "database_name": "ERP", "chain_depth": 2, "wait_type": "LCK_M_S"},
            {"blocked_spid": 102, "blocking_spid": 101, "wait_seconds": 200.0,
             "database_name": "ERP", "chain_depth": 3, "wait_type": "LCK_M_X"},
        ])}
        finding = find(evaluate(make_input(modern_features, frames=frames)), "block_seconds")
        assert finding.severity == "crit" and finding.observed == 200.0

    def test_any_deadlock_is_worth_surfacing(self, modern_features):
        finding = find(evaluate(make_input(modern_features, deadlock_count_24h=1)), "deadlock_count_24h")
        assert finding and finding.severity == "warn"

    def test_a_burst_of_deadlocks_crits(self, modern_features):
        finding = find(evaluate(make_input(modern_features, deadlock_count_24h=25)), "deadlock_count_24h")
        assert finding.severity == "crit"

    def test_no_deadlocks_no_finding(self, modern_features):
        assert find(evaluate(make_input(modern_features, deadlock_count_24h=0)), "deadlock_count_24h") is None


class TestIndexAndStatsAreInformational:
    def test_index_findings_are_info_not_alerts(self, modern_features):
        frames = {"index_frag": pd.DataFrame([
            {"database_name": "ERP", "table_name": "Orders", "index_name": "IX",
             "avg_fragmentation_pct": 55.0, "page_count": 20000}
        ])}
        finding = find(evaluate(make_input(modern_features, frames=frames)), "index_fragmentation")
        # Index maintenance is report material, never a 3 a.m. page.
        assert finding.severity == "info"

    def test_stale_stats_are_info(self, modern_features):
        frames = {"stats_stale": pd.DataFrame([
            {"database_name": "ERP", "table_name": "Orders", "stats_name": "IX",
             "days_since_update": 30, "modification_ratio": 0.4, "is_estimate": False}
        ])}
        assert find(evaluate(make_input(modern_features, frames=frames)), "stale_statistics").severity == "info"

    def test_estimated_stats_are_labelled(self, legacy_features):
        frames = {"stats_stale": pd.DataFrame([
            {"database_name": "LEGACY", "table_name": "Orders", "stats_name": "IX",
             "days_since_update": 30, "modification_ratio": 0.4, "is_estimate": True}
        ])}
        finding = find(evaluate(make_input(legacy_features, frames=frames)), "stale_statistics")
        assert "estimates" in finding.message

    def test_auto_update_off_escalates_above_info(self, modern_features):
        # The stale statistic is the symptom; auto-update being off is the cause.
        modern_features.databases[0].is_auto_update_stats_on = False
        frames = {"stats_stale": pd.DataFrame([
            {"database_name": "ERP", "table_name": "Orders", "stats_name": "IX",
             "days_since_update": 30, "modification_ratio": 0.4, "is_estimate": False}
        ])}
        finding = find(evaluate(make_input(modern_features, frames=frames)), "auto_update_stats_off")
        assert finding and finding.severity == "warn"


class TestFingerprints:
    def test_the_same_problem_keeps_the_same_fingerprint(self, modern_features):
        frames = {"space_db_sample": pd.DataFrame([{
            "database_name": "ERP", "logical_name": "ERP_Data", "free_pct": 5.0,
            "free_mb": 500, "size_mb": 10000,
        }])}
        first = find(evaluate(make_input(modern_features, frames=frames)), "db_free_pct")
        second = find(evaluate(make_input(modern_features, frames=frames)), "db_free_pct")
        assert first.fingerprint == second.fingerprint

    def test_different_objects_get_different_fingerprints(self, modern_features):
        frames = {"space_db_sample": pd.DataFrame([
            {"database_name": "ERP", "logical_name": "ERP_Data", "free_pct": 5.0,
             "free_mb": 1, "size_mb": 10},
            {"database_name": "CRM", "logical_name": "CRM_Data", "free_pct": 4.0,
             "free_mb": 1, "size_mb": 10},
        ])}
        prints = {f.fingerprint for f in evaluate(make_input(modern_features, frames=frames))
                  if f.metric == "db_free_pct"}
        assert len(prints) == 2


class TestUnreachable:
    def test_silence_is_always_critical(self):
        server = ServerConfig(name="PRD-SQL-09", host="h")
        finding = unreachable(server, 9, "run", "Login timeout expired", NOW)
        assert finding.severity == "crit"
        assert finding.category == "availability"
        assert "PRD-SQL-09" in finding.fingerprint
