"""The analyzer: metrics in, findings out.

Effective thresholds are resolved by precedence -- fleet defaults, then any tag override, then the
server override (``config.Thresholds.effective``). Each breach becomes a :class:`Finding` with a
stable ``fingerprint`` (``server|category|metric|object``) that the alert router dedups on, so the
same full disk does not page every 15 minutes.

Fast-tier categories (cpu, memory, io, space, blocking, availability) are alertable. Index, stats
and query findings are ``info``: they are digested into the daily report rather than paged, because
a fragmented index at 03:00 is not an incident.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from ..config import ServerConfig
from ..version import ServerFeatures
from .derive import days_to_full, sustained_average

SEVERITY_ORDER = {"info": 0, "warn": 1, "crit": 2}
CATEGORIES = ["cpu", "memory", "io", "space", "index", "stats", "query", "blocking", "deadlock", "availability"]


@dataclass
class Finding:
    server_name: str
    server_id: int
    run_id: str
    category: str
    severity: str
    metric: str
    message: str
    observed: float | None = None
    threshold: float | None = None
    details: dict[str, Any] = field(default_factory=dict)
    created_utc: datetime | None = None

    @property
    def fingerprint(self) -> str:
        """Stable identity for dedup: the same problem on the same object keeps the same key."""
        obj = self.details.get("object") or ""
        return f"{self.server_name}|{self.category}|{self.metric}|{obj}"[:200]

    def as_row(self) -> dict:
        return {
            "run_id": self.run_id,
            "server_id": self.server_id,
            "created_utc": self.created_utc,
            "category": self.category,
            "severity": self.severity,
            "metric": self.metric,
            "observed": self.observed,
            "threshold": self.threshold,
            "message": self.message[:1000],
            "details_json": json.dumps(self.details, default=str),
            "fingerprint": self.fingerprint,
        }


@dataclass
class AnalysisInput:
    """Everything the analyzer needs. Assembled by the runner; the analyzer itself touches no DB."""

    server: ServerConfig
    server_id: int
    run_id: str
    features: ServerFeatures
    thresholds: dict[str, dict[str, Any]]
    now: datetime
    frames: dict[str, pd.DataFrame] = field(default_factory=dict)
    # History pulled from the repository, oldest first, current sample last.
    cpu_history: list[float | None] = field(default_factory=list)
    grants_history: list[float | None] = field(default_factory=list)
    # {(database, logical_name): (points, capacity_mb)} and {mount_point: (points, capacity_mb)}
    db_growth: dict[tuple[str, str], tuple[list[tuple[datetime, float]], float | None]] = field(default_factory=dict)
    drive_growth: dict[str, tuple[list[tuple[datetime, float]], float | None]] = field(default_factory=dict)
    deadlock_count_24h: int = 0

    def frame(self, table: str) -> pd.DataFrame:
        return self.frames.get(table, pd.DataFrame())

    def limit(self, category: str, key: str, default=None):
        return (self.thresholds.get(category) or {}).get(key, default)


def evaluate(inp: AnalysisInput) -> list[Finding]:
    """Run every rule. Order is severity-independent; the caller sorts."""
    findings: list[Finding] = []
    for rule in (
        _cpu_rules,
        _memory_rules,
        _io_rules,
        _space_db_rules,
        _space_drive_rules,
        _blocking_rules,
        _deadlock_rules,
        _index_rules,
        _stats_rules,
    ):
        findings.extend(rule(inp))
    for finding in findings:
        finding.created_utc = finding.created_utc or inp.now
    return sorted(findings, key=lambda f: -SEVERITY_ORDER.get(f.severity, 0))


def unreachable(server: ServerConfig, server_id: int, run_id: str, error: str, now: datetime) -> Finding:
    """A server that did not answer this run. Always crit -- silence is not health."""
    return Finding(
        server_name=server.name,
        server_id=server_id,
        run_id=run_id,
        category="availability",
        severity="crit",
        metric="unreachable",
        message=f"{server.name} did not respond this run: {error}",
        details={"object": server.address, "error": error},
        created_utc=now,
    )


# ------------------------------------------------------------------------------------------ CPU


def _cpu_rules(inp: AnalysisInput) -> list[Finding]:
    findings: list[Finding] = []
    samples = inp.limit("cpu", "sustained_samples", 4)
    warn = inp.limit("cpu", "sustained_pct_warn")
    crit = inp.limit("cpu", "sustained_pct_crit")

    average = sustained_average(inp.cpu_history, samples)
    if average is not None:
        severity = _severity(average, warn, crit)
        if severity:
            findings.append(
                Finding(
                    inp.server.name, inp.server_id, inp.run_id, "cpu", severity, "sql_cpu_pct",
                    f"SQL Server CPU averaged {average:.0f}% over the last {samples} samples",
                    observed=average,
                    threshold=crit if severity == "crit" else warn,
                    details={"object": "instance", "samples": samples},
                )
            )

    signal_warn = inp.limit("cpu", "signal_wait_pct_warn")
    cpu_frame = inp.frame("cpu_sample")
    if signal_warn is not None and not cpu_frame.empty:
        signal = _scalar(cpu_frame, "signal_wait_pct")
        if signal is not None and signal >= signal_warn:
            findings.append(
                Finding(
                    inp.server.name, inp.server_id, inp.run_id, "cpu", "warn", "signal_wait_pct",
                    f"Signal waits are {signal:.0f}% of total wait time -- threads are ready but "
                    f"queuing for a scheduler",
                    observed=signal, threshold=signal_warn, details={"object": "instance"},
                )
            )
    return findings


# --------------------------------------------------------------------------------------- memory


def _memory_rules(inp: AnalysisInput) -> list[Finding]:
    findings: list[Finding] = []
    frame = inp.frame("memory_sample")
    if frame.empty:
        return findings

    ple = _scalar(frame, "page_life_expectancy")
    floor = _scalar(frame, "ple_dynamic_floor")
    ple_warn = inp.limit("memory", "ple_warn")
    ple_crit = inp.limit("memory", "ple_crit")

    if ple is not None:
        # The dynamic floor scales with buffer pool size; ple_warn is the absolute lower bound, so
        # the effective warning level is whichever is higher.
        effective_warn = max(filter(None, [ple_warn, floor])) if (ple_warn or floor) else None
        if ple_crit is not None and ple < ple_crit:
            findings.append(
                Finding(
                    inp.server.name, inp.server_id, inp.run_id, "memory", "crit", "page_life_expectancy",
                    f"Page life expectancy is {ple:.0f}s (critical below {ple_crit}s)",
                    observed=ple, threshold=float(ple_crit), details={"object": "instance", "dynamic_floor": floor},
                )
            )
        elif effective_warn is not None and ple < effective_warn:
            basis = "buffer-pool-scaled floor" if floor and effective_warn == floor else "absolute floor"
            findings.append(
                Finding(
                    inp.server.name, inp.server_id, inp.run_id, "memory", "warn", "page_life_expectancy",
                    f"Page life expectancy is {ple:.0f}s, below the {basis} of {effective_warn:.0f}s",
                    observed=ple, threshold=float(effective_warn),
                    details={"object": "instance", "dynamic_floor": floor},
                )
            )

    # A single node starving is invisible in the instance-level figure.
    node_ple = _scalar(frame, "min_node_ple")
    if node_ple is not None and ple is not None and ple_crit is not None:
        if node_ple < ple_crit <= ple:
            findings.append(
                Finding(
                    inp.server.name, inp.server_id, inp.run_id, "memory", "warn", "min_node_ple",
                    f"Lowest NUMA node PLE is {node_ple:.0f}s while the instance figure is {ple:.0f}s "
                    f"-- one node is under pressure",
                    observed=node_ple, threshold=float(ple_crit), details={"object": "numa"},
                )
            )

    grants_warn = inp.limit("memory", "memory_grants_pending_warn")
    if grants_warn is not None and inp.grants_history:
        recent = [g for g in inp.grants_history[-2:] if g is not None]
        # ">0 sustained": one blip is normal, two consecutive samples is pressure.
        if len(recent) == 2 and all(g >= grants_warn for g in recent):
            findings.append(
                Finding(
                    inp.server.name, inp.server_id, inp.run_id, "memory", "warn", "memory_grants_pending",
                    f"{recent[-1]:.0f} memory grants pending across consecutive samples -- queries are "
                    f"waiting for workspace memory",
                    observed=float(recent[-1]), threshold=float(grants_warn), details={"object": "instance"},
                )
            )
    return findings


# ------------------------------------------------------------------------------------------- IO


def _io_rules(inp: AnalysisInput) -> list[Finding]:
    findings: list[Finding] = []
    frame = inp.frame("io_file_sample")
    if frame.empty:
        return findings

    for direction, column in (("read", "interval_read_latency_ms"), ("write", "interval_write_latency_ms")):
        warn = inp.limit("io", f"{direction}_latency_ms_warn")
        crit = inp.limit("io", f"{direction}_latency_ms_crit")
        if warn is None and crit is None:
            continue
        if column not in frame:
            continue
        for _, row in frame.iterrows():
            latency = _number(row.get(column))
            if latency is None:
                continue  # no IO in the window -- no latency to judge
            severity = _severity(latency, warn, crit)
            if not severity:
                continue
            target = row.get("physical_name") or row.get("database_name") or "?"
            findings.append(
                Finding(
                    inp.server.name, inp.server_id, inp.run_id, "io", severity, f"{direction}_latency_ms",
                    f"{direction.capitalize()} latency {latency:.0f} ms on {target} "
                    f"({row.get('database_name')})",
                    observed=latency, threshold=crit if severity == "crit" else warn,
                    details={"object": target, "database": row.get("database_name"),
                             "file_type": row.get("file_type")},
                )
            )
    return findings


# ---------------------------------------------------------------------------------------- space


def _space_db_rules(inp: AnalysisInput) -> list[Finding]:
    findings: list[Finding] = []
    frame = inp.frame("space_db_sample")
    warn = inp.limit("space", "db_free_pct_warn")
    crit = inp.limit("space", "db_free_pct_crit")

    if not frame.empty and (warn is not None or crit is not None):
        for _, row in frame.iterrows():
            free_pct = _number(row.get("free_pct"))
            if free_pct is None:
                continue
            # An autogrowing file with headroom on the volume is not the same as a capped one, but
            # a file that is nearly full is still worth surfacing either way.
            severity = _severity_low(free_pct, warn, crit)
            if not severity:
                continue
            target = f"{row.get('database_name')}/{row.get('logical_name')}"
            findings.append(
                Finding(
                    inp.server.name, inp.server_id, inp.run_id, "space", severity, "db_free_pct",
                    f"{target} is {free_pct:.0f}% free ({row.get('free_mb')} MB of {row.get('size_mb')} MB)",
                    observed=free_pct, threshold=crit if severity == "crit" else warn,
                    details={"object": target, "database": row.get("database_name")},
                )
            )

    findings.extend(_days_to_full_findings(inp, inp.db_growth, "db", lambda key: f"{key[0]}/{key[1]}"))
    return findings


def _space_drive_rules(inp: AnalysisInput) -> list[Finding]:
    findings: list[Finding] = []
    frame = inp.frame("space_drive_sample")
    if frame.empty:
        return findings

    has_pct = inp.features.has_volume_stats
    if has_pct:
        warn, crit, metric = (
            inp.limit("space", "drive_free_pct_warn"),
            inp.limit("space", "drive_free_pct_crit"),
            "drive_free_pct",
        )
    else:
        # No sys.dm_os_volume_stats means no volume total, so there is no percentage to threshold
        # against. Alerting switches to absolute free MB rather than inventing a denominator.
        warn, crit, metric = (
            inp.limit("space", "drive_free_mb_warn"),
            inp.limit("space", "drive_free_mb_crit"),
            "drive_free_mb",
        )

    for _, row in frame.iterrows():
        mount = row.get("volume_mount_point") or "?"
        if has_pct:
            observed = _number(row.get("free_pct"))
            rendered = f"{observed:.0f}% free" if observed is not None else None
        else:
            free_gb = _number(row.get("free_gb"))
            observed = None if free_gb is None else free_gb * 1024
            rendered = f"{free_gb:.1f} GB free (free % unavailable on this version)" if free_gb is not None else None
        if observed is None:
            continue
        severity = _severity_low(observed, warn, crit)
        if not severity:
            continue
        findings.append(
            Finding(
                inp.server.name, inp.server_id, inp.run_id, "space", severity, metric,
                f"Volume {mount}: {rendered}",
                observed=observed, threshold=crit if severity == "crit" else warn,
                details={"object": mount, "legacy_free_mb_only": not has_pct},
            )
        )

    findings.extend(_days_to_full_findings(inp, inp.drive_growth, "drive", lambda key: str(key)))
    return findings


def _days_to_full_findings(inp, growth, kind: str, label) -> list[Finding]:
    threshold = inp.limit("space", "days_to_full_warn")
    if threshold is None or not growth:
        return []
    findings = []
    for key, (points, capacity) in growth.items():
        projected = days_to_full(points, capacity)
        if projected is None or projected > threshold:
            continue
        severity = "crit" if projected <= threshold / 2 else "warn"
        target = label(key)
        findings.append(
            Finding(
                inp.server.name, inp.server_id, inp.run_id, "space", severity, f"{kind}_days_to_full",
                f"{target} is projected full in ~{projected:.0f} days at the current growth rate",
                observed=projected, threshold=float(threshold),
                details={"object": target, "projection": "linear fit over the retained window"},
            )
        )
    return findings


# ------------------------------------------------------------------------- blocking & deadlocks


def _blocking_rules(inp: AnalysisInput) -> list[Finding]:
    frame = inp.frame("blocking_event")
    if frame.empty:
        return []
    warn = inp.limit("blocking", "block_seconds_warn")
    crit = inp.limit("blocking", "block_seconds_crit")
    if warn is None and crit is None:
        return []

    worst = frame.loc[frame["wait_seconds"].astype(float).idxmax()] if "wait_seconds" in frame else None
    if worst is None:
        return []
    seconds = _number(worst.get("wait_seconds"))
    severity = _severity(seconds, warn, crit)
    if not severity or seconds is None:
        return []
    depth = worst.get("chain_depth")
    return [
        Finding(
            inp.server.name, inp.server_id, inp.run_id, "blocking", severity, "block_seconds",
            f"Session {worst.get('blocked_spid')} blocked {seconds:.0f}s by {worst.get('blocking_spid')} "
            f"in {worst.get('database_name')} (chain depth {depth})",
            observed=seconds, threshold=crit if severity == "crit" else warn,
            details={"object": str(worst.get("database_name")), "chain_depth": int(depth or 1),
                     "wait_type": worst.get("wait_type")},
        )
    ]


def _deadlock_rules(inp: AnalysisInput) -> list[Finding]:
    warn = inp.limit("deadlock", "count_24h_warn")
    crit = inp.limit("deadlock", "count_24h_crit")
    count = inp.deadlock_count_24h
    severity = _severity(count, warn, crit)
    if not severity or not count:
        return []
    return [
        Finding(
            inp.server.name, inp.server_id, inp.run_id, "deadlock", severity, "deadlock_count_24h",
            f"{count} deadlock(s) in the last 24h" + (" -- a burst usually means one hot code path"
                                                      if severity == "crit" else ""),
            observed=float(count), threshold=float(crit if severity == "crit" else warn),
            details={"object": "instance"},
        )
    ]


# ------------------------------------------------------------- index & stats (digest, not paged)


def _index_rules(inp: AnalysisInput) -> list[Finding]:
    findings: list[Finding] = []
    min_report = inp.limit("index", "frag_pct_min_report", 15)
    min_pages = inp.limit("index", "min_page_count", 1000)

    frag = inp.frame("index_frag")
    if not frag.empty:
        candidates = frag[
            (frag["avg_fragmentation_pct"].astype(float) >= float(min_report))
            & (frag["page_count"].astype(float) >= float(min_pages))
        ]
        if not candidates.empty:
            worst = candidates.iloc[0]
            findings.append(
                Finding(
                    inp.server.name, inp.server_id, inp.run_id, "index", "info", "index_fragmentation",
                    f"{len(candidates)} index(es) above {min_report}% fragmentation; worst is "
                    f"{worst.get('table_name')}.{worst.get('index_name')} at "
                    f"{_number(worst.get('avg_fragmentation_pct')):.0f}%",
                    observed=float(len(candidates)), threshold=float(min_report),
                    details={"object": "indexes", "count": int(len(candidates))},
                )
            )

    missing = inp.frame("index_missing")
    if not missing.empty:
        findings.append(
            Finding(
                inp.server.name, inp.server_id, inp.run_id, "index", "info", "missing_indexes",
                f"{len(missing)} missing-index suggestion(s) -- ranked estimates only, the DMV "
                f"over-recommends and ignores write cost",
                observed=float(len(missing)), details={"object": "indexes"},
            )
        )

    unused = inp.frame("index_unused")
    if not unused.empty:
        findings.append(
            Finding(
                inp.server.name, inp.server_id, inp.run_id, "index", "info", "unused_indexes",
                f"{len(unused)} nonclustered index(es) with writes but no reads since the last "
                f"counter reset -- read against instance uptime before dropping",
                observed=float(len(unused)), details={"object": "indexes"},
            )
        )
    return findings


def _stats_rules(inp: AnalysisInput) -> list[Finding]:
    frame = inp.frame("stats_stale")
    if frame.empty:
        return []

    stale_days = inp.limit("stats", "stale_days_warn", 7)
    ratio_warn = inp.limit("stats", "modification_ratio_warn", 0.20)
    estimated = bool(frame.get("is_estimate", pd.Series([False])).any())
    qualifier = " (modification counts are estimates on this version)" if estimated else ""

    findings = [
        Finding(
            inp.server.name, inp.server_id, inp.run_id, "stats", "info", "stale_statistics",
            f"{len(frame)} statistic(s) older than {stale_days} days or past a "
            f"{float(ratio_warn):.0%} modification ratio{qualifier}",
            observed=float(len(frame)), threshold=float(stale_days),
            details={"object": "statistics", "is_estimate": estimated},
        )
    ]

    # Escalation: stale stats on a database with auto-update OFF is a cause, not a symptom.
    auto_off = {db.name for db in inp.features.databases if not db.is_auto_update_stats_on}
    affected = sorted(auto_off & set(frame["database_name"].dropna().unique())) if "database_name" in frame else []
    if affected:
        findings.append(
            Finding(
                inp.server.name, inp.server_id, inp.run_id, "stats", "warn", "auto_update_stats_off",
                f"Auto-update statistics is OFF on {', '.join(affected[:5])}"
                + (f" and {len(affected) - 5} more" if len(affected) > 5 else "")
                + " -- stale statistics there will not self-correct",
                observed=float(len(affected)),
                details={"object": "databases", "databases": affected},
            )
        )
    return findings


# ----------------------------------------------------------------------------------- primitives


def _severity(value, warn, crit) -> str | None:
    """Higher is worse."""
    if value is None:
        return None
    if crit is not None and value >= crit:
        return "crit"
    if warn is not None and value >= warn:
        return "warn"
    return None


def _severity_low(value, warn, crit) -> str | None:
    """Lower is worse (free space)."""
    if value is None:
        return None
    if crit is not None and value <= crit:
        return "crit"
    if warn is not None and value <= warn:
        return "warn"
    return None


def _scalar(frame: pd.DataFrame, column: str) -> float | None:
    if frame.empty or column not in frame:
        return None
    return _number(frame.iloc[0].get(column))


def _number(value) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
