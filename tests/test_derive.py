"""Rate math over fixed sample pairs -- the part of the project most easily wrong and least visible."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from sqlhealthwatch.analyze.derive import (
    counter_delta,
    days_to_full,
    derive_io_intervals,
    dynamic_ple_floor,
    growth_slope_mb_per_day,
    interval_latency_ms,
    interval_throughput_mb_s,
    sustained_average,
)


class TestPleFloor:
    def test_scales_with_buffer_pool(self):
        # 64 GB target -> (64/4) * 300 = 4800s, not the folklore 300.
        assert dynamic_ple_floor(64 * 1024) == 4800

    def test_small_server_lands_near_the_classic_value(self):
        assert dynamic_ple_floor(4 * 1024) == 300

    def test_unknown_target_has_no_floor(self):
        assert dynamic_ple_floor(None) is None
        assert dynamic_ple_floor(0) is None


class TestCounterDelta:
    def test_normal_progression(self):
        assert counter_delta(100, 150) == 50

    def test_restart_is_reported_as_unknown_not_negative(self):
        # A cumulative counter going backwards means the instance restarted.
        assert counter_delta(500, 20) is None

    def test_missing_reading(self):
        assert counter_delta(None, 10) is None


class TestIntervalLatency:
    def test_latency_is_stall_over_operations(self):
        # 4000 ms of stall across 200 reads in the window = 20 ms per read.
        assert interval_latency_ms(1000, 5000, 800, 1000) == 20.0

    def test_no_io_in_the_window_is_none_not_zero(self):
        # Zero latency would read as an unusually fast disk; there simply was no IO.
        assert interval_latency_ms(1000, 1000, 800, 800) is None

    def test_counter_reset_yields_none(self):
        assert interval_latency_ms(5000, 10, 1000, 5) is None


class TestThroughput:
    def test_megabytes_per_second(self):
        one_hundred_mb = 100 * 1024 * 1024
        assert interval_throughput_mb_s(0, one_hundred_mb, 10) == 10.0

    def test_zero_elapsed(self):
        assert interval_throughput_mb_s(0, 1024, 0) is None


class TestDeriveIoIntervals:
    def _frame(self, when, reads, stall, bytes_read):
        return pd.DataFrame(
            [
                {
                    "collected_at_utc": when,
                    "database_name": "ERP",
                    "physical_name": "E:\\data\\erp.mdf",
                    "num_of_reads": reads,
                    "num_of_writes": 10,
                    "io_stall_read_ms": stall,
                    "io_stall_write_ms": 100,
                    "bytes_read": bytes_read,
                    "bytes_written": 0,
                }
            ]
        )

    def test_fills_interval_columns_from_the_previous_sample(self):
        before = self._frame(datetime(2026, 8, 30, 6, 0), 1000, 10000, 0)
        now = self._frame(datetime(2026, 8, 30, 6, 15), 1500, 25000, 900 * 1024 * 1024)

        result = derive_io_intervals(now, before)
        row = result.iloc[0]

        assert row["interval_read_latency_ms"] == 30.0  # 15000 ms / 500 reads
        assert row["interval_read_mb_s"] == 1.0  # 900 MB over 900 seconds

    def test_first_ever_sample_has_no_interval(self):
        now = self._frame(datetime(2026, 8, 30, 6, 15), 1500, 25000, 0)
        result = derive_io_intervals(now, None)
        assert result.iloc[0]["interval_read_latency_ms"] is None

    def test_new_file_since_last_sample_is_skipped_not_guessed(self):
        before = self._frame(datetime(2026, 8, 30, 6, 0), 1000, 10000, 0)
        now = self._frame(datetime(2026, 8, 30, 6, 15), 1500, 25000, 0)
        now.at[0, "physical_name"] = "E:\\data\\new_file.ndf"

        result = derive_io_intervals(now, before)
        assert result.iloc[0]["interval_read_latency_ms"] is None


class TestGrowthProjection:
    def _series(self, values, start=datetime(2026, 8, 24)):
        return [(start + timedelta(days=i), v) for i, v in enumerate(values)]

    def test_slope_of_steady_growth(self):
        assert growth_slope_mb_per_day(self._series([100, 200, 300, 400])) == pytest.approx(100.0)

    def test_days_to_full_at_current_rate(self):
        # 400 MB used of 1000, growing 100 MB/day -> 6 days of headroom.
        assert days_to_full(self._series([100, 200, 300, 400]), 1000) == pytest.approx(6.0)

    def test_flat_file_is_not_projectable(self):
        assert days_to_full(self._series([500, 500, 500]), 1000) is None

    def test_shrinking_file_is_not_projectable(self):
        assert days_to_full(self._series([500, 400, 300]), 1000) is None

    def test_already_full(self):
        assert days_to_full(self._series([900, 1000, 1100]), 1000) == 0.0

    def test_unbounded_file_has_no_projection(self):
        # An unlimited autogrow file is bounded by its volume, not by itself.
        assert days_to_full(self._series([100, 200]), None) is None

    def test_single_point_is_not_a_trend(self):
        assert growth_slope_mb_per_day(self._series([100])) is None


class TestSustainedAverage:
    def test_average_of_the_window(self):
        assert sustained_average([50, 60, 90, 95, 100], 4) == 86.25

    def test_requires_the_full_window(self):
        # Two samples out of a required four is not yet "sustained".
        assert sustained_average([95, 99], 4) is None

    def test_ignores_gaps(self):
        assert sustained_average([None, 80, 80, 80, 80], 4) == 80.0
