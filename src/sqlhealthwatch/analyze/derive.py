"""Derived metrics.

Most of what this project reads is a counter that has been accumulating since the instance last
restarted. A since-restart average answers "what has this server been like for months", which is
almost never the question. The functions here turn consecutive samples into *interval* rates -- IO
latency and throughput over the last 15 minutes, growth slope over the retained window -- so a spike
appears as a spike.

Every function is pure and takes plain values or DataFrames, so the rate math is unit tested with
fixed sample pairs and no database.

Counter resets are handled explicitly: if the current cumulative value is lower than the previous
one the instance restarted (or the DMV was cleared), and the interval is reported as unknown rather
than as a negative or absurdly large rate.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

# The classic "PLE should be over 300" rule predates servers with more than 4 GB of buffer pool.
PLE_BASELINE_SECONDS = 300
PLE_BASELINE_GB = 4

IO_KEY = ["database_name", "physical_name"]


def dynamic_ple_floor(target_server_memory_mb: int | None) -> int | None:
    """Scale the PLE floor by buffer pool size: (target GB / 4) x 300.

    A flat 300 on a 256 GB box means the whole buffer pool churns every five minutes and still
    reads as healthy.
    """
    if not target_server_memory_mb:
        return None
    target_gb = target_server_memory_mb / 1024.0
    return int(round((target_gb / PLE_BASELINE_GB) * PLE_BASELINE_SECONDS))


def counter_delta(previous, current) -> float | None:
    """Delta between two cumulative counter readings, or None if the counter reset."""
    if previous is None or current is None:
        return None
    try:
        previous, current = float(previous), float(current)
    except (TypeError, ValueError):
        return None
    if current < previous:
        return None  # instance restarted or the DMV was cleared -- this interval is unknown
    return current - previous


def interval_latency_ms(prev_stall_ms, curr_stall_ms, prev_ops, curr_ops) -> float | None:
    """Latency over the interval = delta stall / delta operations.

    Returns None when nothing happened in the window: zero IO has no latency, and reporting 0 ms
    would look like an unusually fast disk.
    """
    stall = counter_delta(prev_stall_ms, curr_stall_ms)
    ops = counter_delta(prev_ops, curr_ops)
    if stall is None or ops is None or ops <= 0:
        return None
    return round(stall / ops, 2)


def interval_throughput_mb_s(prev_bytes, curr_bytes, seconds: float) -> float | None:
    """Throughput over the interval in MB/s."""
    delta = counter_delta(prev_bytes, curr_bytes)
    if delta is None or seconds <= 0:
        return None
    return round(delta / seconds / 1024 / 1024, 2)


def derive_io_intervals(current: pd.DataFrame, previous: pd.DataFrame | None) -> pd.DataFrame:
    """Fill the interval_* columns on an io_file_sample frame from the previous sample.

    Files are matched on (database_name, physical_name). A file that appeared since the last sample
    simply has no interval yet -- it gets one on the next run.
    """
    result = current.copy()
    interval_columns = [
        "interval_read_latency_ms",
        "interval_write_latency_ms",
        "interval_read_mb_s",
        "interval_write_mb_s",
    ]
    for column in interval_columns:
        if column not in result:
            result[column] = None

    if previous is None or previous.empty or current.empty:
        return result

    seconds = _elapsed_seconds(previous, current)
    if seconds is None or seconds <= 0:
        return result

    prior = previous.set_index(IO_KEY, drop=False)
    for position, row in result.iterrows():
        key = (row.get("database_name"), row.get("physical_name"))
        if key not in prior.index:
            continue
        before = prior.loc[key]
        if isinstance(before, pd.DataFrame):  # duplicate physical names -- take the first
            before = before.iloc[0]

        result.at[position, "interval_read_latency_ms"] = interval_latency_ms(
            before.get("io_stall_read_ms"), row.get("io_stall_read_ms"),
            before.get("num_of_reads"), row.get("num_of_reads"),
        )
        result.at[position, "interval_write_latency_ms"] = interval_latency_ms(
            before.get("io_stall_write_ms"), row.get("io_stall_write_ms"),
            before.get("num_of_writes"), row.get("num_of_writes"),
        )
        result.at[position, "interval_read_mb_s"] = interval_throughput_mb_s(
            before.get("bytes_read"), row.get("bytes_read"), seconds
        )
        result.at[position, "interval_write_mb_s"] = interval_throughput_mb_s(
            before.get("bytes_written"), row.get("bytes_written"), seconds
        )
    return result


def _elapsed_seconds(previous: pd.DataFrame, current: pd.DataFrame) -> float | None:
    try:
        before = pd.to_datetime(previous["collected_at_utc"]).max()
        after = pd.to_datetime(current["collected_at_utc"]).max()
    except (KeyError, ValueError, TypeError):
        return None
    if pd.isna(before) or pd.isna(after):
        return None
    return (after - before).total_seconds()


def growth_slope_mb_per_day(points: list[tuple[datetime, float]]) -> float | None:
    """Least-squares slope of used MB over time, in MB/day.

    Two points give a straight line; more points smooth out a single large load. A flat or shrinking
    trend returns <= 0, which the caller reads as "not filling".
    """
    usable = [(t, float(v)) for t, v in points if t is not None and v is not None]
    if len(usable) < 2:
        return None

    base = min(t for t, _ in usable)
    xs = [(t - base).total_seconds() / 86400.0 for t, _ in usable]
    ys = [v for _, v in usable]
    n = len(xs)
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator == 0:
        return None
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denominator


def days_to_full(points: list[tuple[datetime, float]], capacity_mb: float | None) -> float | None:
    """Project days until a file or volume fills, at the current growth rate.

    None means "not projectable": no capacity known, no trend, or not growing.
    """
    if not capacity_mb or capacity_mb <= 0:
        return None
    slope = growth_slope_mb_per_day(points)
    if slope is None or slope <= 0:
        return None
    current_used = points[-1][1]
    headroom = capacity_mb - float(current_used)
    if headroom <= 0:
        return 0.0
    return round(headroom / slope, 1)


def sustained_average(values: list[float | None], samples: int) -> float | None:
    """Average of the last N non-null samples, used for "sustained CPU" rather than one spike.

    Requires the full N: two samples out of a required four is not yet sustained.
    """
    usable = [float(v) for v in values if v is not None]
    if len(usable) < samples or samples <= 0:
        return None
    window = usable[-samples:]
    return round(sum(window) / len(window), 2)
