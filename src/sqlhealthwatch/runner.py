"""Tier orchestration: fan out over the fleet, collect, store, analyze, alert.

Concurrency model: database IO is blocking, so a bounded ``ThreadPoolExecutor`` (8-10 workers) is
the right shape -- not asyncio. Each worker owns its own connections, both to the monitored instance
and to the repository, because pyodbc connections are not shareable across threads. Since SQL Server
has no single-writer limit, those workers bulk-insert concurrently without contending.

Failure isolation is the point of this module. A server that times out, refuses a login or drops the
network is caught, recorded in ``mon.server_status``, raised as a crit availability alert, and left
behind -- the other 39 servers finish their run.

Per-server time budget is enforced two ways: every query carries a cursor timeout, and the worker
checks its deadline between collectors. A thread cannot be forcibly killed in Python, so a hung
query is bounded by the driver timeout rather than by the pool.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from .analyze import thresholds as analyzer
from .analyze.derive import derive_io_intervals
from .alerting.router import AlertRouter
from .collectors import ALL_COLLECTORS, Collector, ServerContext, collectors_for
from .collectors.base import load_sql
from .config import AppConfig, ServerConfig
from .connection import SqlConnection, connect
from .storage.parquet_export import export_frame
from .storage.repository import Repository
from .util.logging import set_run_id
from .util.timeutil import utcnow
from .version import ServerFeatures, databases_sql, parse_database_rows, parse_probe_row

log = logging.getLogger(__name__)


@dataclass
class ServerResult:
    server: ServerConfig
    ok: bool
    duration_ms: int
    error: str | None = None
    rows_written: dict[str, int] = field(default_factory=dict)
    findings: list = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    skipped_collectors: list[str] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return sum(self.rows_written.values())


@dataclass
class RunResult:
    run_id: str
    tier: str
    started: datetime
    finished: datetime | None = None
    results: list[ServerResult] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str | None = None

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.results if not r.ok)

    @property
    def findings(self) -> list:
        return [f for r in self.results for f in r.findings]

    def summary(self) -> str:
        if self.skipped:
            return f"{self.tier} run skipped: {self.skip_reason}"
        rows = sum(r.total_rows for r in self.results)
        crit = sum(1 for f in self.findings if f.severity == "crit")
        warn = sum(1 for f in self.findings if f.severity == "warn")
        seconds = (self.finished - self.started).total_seconds() if self.finished else 0
        return (
            f"{self.tier} run {self.run_id[:8]}: {self.ok_count} ok, {self.failed_count} failed, "
            f"{rows} rows, {crit} crit / {warn} warn findings, {seconds:.0f}s"
        )


def run_tier(config: AppConfig, tier: str, only: list[str] | None = None, dry_run: bool = False) -> RunResult:
    """Run one tier across the fleet."""
    settings = config.settings
    servers = [s for s in config.inventory.enabled if not only or s.name in only]
    if not servers:
        raise ValueError("no enabled servers matched" + (f": {only}" if only else ""))

    with Repository(settings.repository, settings) as repo:
        if settings.repository.auto_bootstrap and not repo.schema_exists():
            log.info("repository schema not found -- bootstrapping")
            repo.bootstrap()

        with repo.tier_lock(tier) as acquired:
            if not acquired:
                # Piling a second fan-out onto the fleet is worse than missing one interval.
                log.warning("previous %s run is still in progress -- skipping this one", tier)
                return RunResult(run_id="-", tier=tier, started=utcnow(), skipped=True,
                                 skip_reason="the previous run of this tier is still running")

            run_id = repo.start_run(tier)
            set_run_id(run_id[:8])
            started = utcnow()
            log.info("%s tier starting over %d server(s)", tier, len(servers))

            # Resolve the dimension rows once on the main connection, so workers never race to
            # create the same server row.
            server_ids = {s.name: repo.ensure_server(s.name, s.host, s.tags) for s in servers}

            results = _fan_out(config, tier, servers, server_ids, dry_run)

            run = RunResult(run_id=run_id, tier=tier, started=started, finished=utcnow(), results=results)
            repo.finish_run(run_id, run.ok_count, run.failed_count, run.summary())
            log.info(run.summary())
            return run


def _fan_out(config: AppConfig, tier: str, servers: list[ServerConfig], server_ids: dict[str, int],
             dry_run: bool) -> list[ServerResult]:
    max_workers = min(config.settings.concurrency.max_workers, len(servers))
    results: list[ServerResult] = []

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="shw") as pool:
        futures = {
            pool.submit(_collect_server, config, tier, server, server_ids[server.name], dry_run): server
            for server in servers
        }
        for future in as_completed(futures):
            server = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:  # a worker should catch its own errors; this is the backstop
                log.exception("%s: unhandled collector error", server.name)
                results.append(ServerResult(server=server, ok=False, duration_ms=0, error=str(exc)))
    return results


def _collect_server(config: AppConfig, tier: str, server: ServerConfig, server_id: int,
                    dry_run: bool) -> ServerResult:
    """One server's whole pass. Every failure inside is contained to this server."""
    started = time.monotonic()
    deadline = started + config.settings.concurrency.per_server_timeout_s
    run_id = "-"

    repo: Repository | None = None
    connection: SqlConnection | None = None
    try:
        repo = Repository(config.settings.repository, config.settings).connect()
        run_id = _current_run_id(repo, tier)
        connection = connect(server, retries=1)
        features = _probe(connection, config, server)
        repo.update_server_features(server_id, features)

        ctx = ServerContext(
            server=server,
            features=features,
            connection=connection,
            settings=config.settings,
            sql_dir=config.sql_dir,
            run_id=run_id,
            server_id=server_id,
            collected_at_utc=utcnow(),
            tier=tier,
            watermarks={"deadlocks": repo.get_watermark(server_id, "deadlocks")},
        )

        frames, skipped = _run_collectors(ctx, tier, repo, deadline)
        written = _persist(ctx, repo, frames, config, tier, dry_run)
        findings = _analyze_and_alert(ctx, repo, config, frames, dry_run)

        duration_ms = int((time.monotonic() - started) * 1000)
        repo.record_server_status(run_id, server_id, True, duration_ms)
        return ServerResult(server=server, ok=True, duration_ms=duration_ms, rows_written=written,
                            findings=findings, notes=ctx.notes, skipped_collectors=skipped)

    except Exception as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        error = str(exc).strip()[:900]
        log.warning("%s: collection failed -- %s", server.name, error)
        findings = []
        if repo is not None:
            try:
                repo.record_server_status(run_id, server_id, False, duration_ms, error)
                finding = analyzer.unreachable(server, server_id, run_id, error, utcnow())
                repo.write_findings([finding])
                findings = [finding]
                if not dry_run:
                    AlertRouter(config.alerts, repo).dispatch([finding], server, server_id)
            except Exception:  # pragma: no cover - the repository itself is unhealthy
                log.exception("%s: could not record the failure", server.name)
        return ServerResult(server=server, ok=False, duration_ms=duration_ms, error=error, findings=findings)

    finally:
        if connection is not None:
            connection.close()
        if repo is not None:
            repo.close()


def _current_run_id(repo: Repository, tier: str) -> str:
    """The run row is created by the parent before fan-out; workers attach to the newest one."""
    row = repo.connection.query_one(
        f"SELECT TOP (1) run_id FROM {repo.table('runs')} WHERE tier = ? ORDER BY started_utc DESC",
        [tier],
    )
    return str(row["run_id"]) if row else "-"


def _probe(connection: SqlConnection, config: AppConfig, server: ServerConfig) -> ServerFeatures:
    """Version and capability detection -- object existence, not a version-to-feature guess."""
    probe_row = connection.query_one(load_sql(config.sql_dir, "feature_probe.sql"))
    features = parse_probe_row(probe_row or {})

    template = load_sql(config.sql_dir, "databases.sql")
    try:
        rows = connection.query(databases_sql(features, template))
        features.databases = parse_database_rows(rows)
    except Exception as exc:
        # Without the database list the per-database collectors have nothing to iterate, but the
        # server-scoped ones still work -- so this degrades rather than fails.
        log.warning("%s: could not enumerate databases (%s)", server.name, exc)
    return features


def _run_collectors(ctx: ServerContext, tier: str, repo: Repository,
                    deadline: float) -> tuple[dict[str, pd.DataFrame], list[str]]:
    frames: dict[str, pd.DataFrame] = {}
    skipped: list[str] = []

    for collector in collectors_for(tier):
        if time.monotonic() > deadline:
            remaining = [c.name for c in collectors_for(tier) if c.name not in frames]
            log.warning("%s: per-server time budget exhausted, skipping %s", ctx.name, ", ".join(remaining))
            ctx.note(f"time budget exhausted -- skipped: {', '.join(remaining)}")
            skipped.extend(remaining)
            break

        if not collector.applies_to(ctx):
            skipped.append(collector.name)
            continue

        try:
            frame = collector.collect(ctx)
        except Exception as exc:
            # One collector failing (a permission gap, an unexpected DMV shape) must not cost the
            # server its other metrics.
            log.warning("%s: collector %s failed -- %s", ctx.name, collector.name, exc)
            ctx.note(f"{collector.name}: failed ({str(exc)[:200]})")
            skipped.append(collector.name)
            continue

        if collector.name == "io_disk" and not frame.empty:
            frame = derive_io_intervals(frame, repo.previous_io_sample(ctx.server_id))

        frames[collector.name] = frame
        if collector.name == "deadlocks" and not frame.empty:
            repo.set_watermark(ctx.server_id, "deadlocks", collector.watermark_from(frame))

    return frames, skipped


def _persist(ctx: ServerContext, repo: Repository, frames: dict[str, pd.DataFrame], config: AppConfig,
             tier: str, dry_run: bool) -> dict[str, int]:
    written: dict[str, int] = {}
    run_date = ctx.collected_at_utc.date().isoformat()
    exports_dir = config.resolve_path(config.settings.paths.exports)

    for collector in ALL_COLLECTORS:
        frame = frames.get(collector.name)
        if frame is None or frame.empty:
            continue

        if collector.table and not dry_run:
            try:
                written[collector.name] = repo.write(collector.table, frame)
            except Exception as exc:
                log.warning("%s: writing %s failed -- %s", ctx.name, collector.table, exc)
                ctx.note(f"{collector.name}: write failed ({str(exc)[:200]})")

        # The export is a cold archive; a failure there is logged inside and never blocks the run.
        if config.settings.collection.parquet_export and not dry_run:
            export_frame(frame, exports_dir, run_date, tier, f"{ctx.name}_{collector.name}", ctx.run_id)

    return written


def _analyze_and_alert(ctx: ServerContext, repo: Repository, config: AppConfig,
                       frames: dict[str, pd.DataFrame], dry_run: bool) -> list:
    """Evaluate thresholds against this run's data plus the history the rules need."""
    effective = config.thresholds.effective(ctx.server)
    by_table = {c.table: frames[c.name] for c in ALL_COLLECTORS if c.table and c.name in frames}

    samples = effective.get("cpu", {}).get("sustained_samples", 4)
    inp = analyzer.AnalysisInput(
        server=ctx.server,
        server_id=ctx.server_id,
        run_id=ctx.run_id,
        features=ctx.features,
        thresholds=effective,
        now=ctx.collected_at_utc,
        frames=by_table,
        cpu_history=repo.recent_values(ctx.server_id, "cpu_sample", "sql_cpu_pct", samples),
        grants_history=repo.recent_values(ctx.server_id, "memory_sample", "memory_grants_pending", 2),
        db_growth=_db_growth(repo, ctx.server_id, config.settings.retention.raw_days),
        drive_growth=_drive_growth(repo, ctx.server_id, config.settings.retention.raw_days),
        deadlock_count_24h=repo.deadlock_count(ctx.server_id, 24),
    )

    findings = analyzer.evaluate(inp)
    if findings and not dry_run:
        try:
            repo.write_findings(findings)
        except Exception as exc:
            log.warning("%s: writing findings failed -- %s", ctx.name, exc)
        AlertRouter(config.alerts, repo).dispatch(findings, ctx.server, ctx.server_id)
    return findings


def _db_growth(repo: Repository, server_id: int, days: int) -> dict:
    """Per-file used-MB series and the capacity that bounds it.

    Only files with a real ``max_size`` get a projection: an unlimited autogrow file is bounded by
    the volume, not by itself, so projecting it would answer the wrong question.
    """
    frame = repo.growth_series(server_id, days)
    if frame.empty:
        return {}
    growth = {}
    for (database, logical), group in frame.groupby(["database_name", "logical_name"]):
        capacity = group["max_size_mb"].dropna()
        if capacity.empty:
            continue
        points = [
            (pd.to_datetime(row["collected_at_utc"]).to_pydatetime(), float(row["used_mb"]))
            for _, row in group.iterrows()
            if row.get("used_mb") is not None
        ]
        if len(points) >= 2:
            growth[(database, logical)] = (points, float(capacity.max()))
    return growth


def _drive_growth(repo: Repository, server_id: int, days: int) -> dict:
    """Per-volume used-MB series, derived from total minus free.

    Skipped entirely on legacy instances, where xp_fixeddrives gives no volume total and there is
    therefore no capacity to project against.
    """
    frame = repo.drive_growth_series(server_id, days)
    if frame.empty:
        return {}
    growth = {}
    for mount, group in frame.groupby("volume_mount_point"):
        usable = group.dropna(subset=["total_gb", "free_gb"])
        if len(usable) < 2:
            continue
        capacity_mb = float(usable["total_gb"].max()) * 1024
        points = [
            (
                pd.to_datetime(row["collected_at_utc"]).to_pydatetime(),
                (float(row["total_gb"]) - float(row["free_gb"])) * 1024,
            )
            for _, row in usable.iterrows()
        ]
        growth[mount] = (points, capacity_mb)
    return growth


def test_connections(config: AppConfig, only: list[str] | None = None) -> list[dict]:
    """Connectivity, permission and feature summary used by the ``test-conn`` command."""
    servers = [s for s in config.inventory.servers if not only or s.name in only]
    report = []
    for server in servers:
        entry = {"server": server.name, "address": server.address, "auth": server.auth,
                 "enabled": server.enabled}
        try:
            with connect(server, retries=0) as connection:
                features = _probe(connection, config, server)
                entry.update(
                    ok=True,
                    version=features.version_name,
                    edition=features.edition,
                    databases=len(features.databases),
                    query_store_databases=len(features.query_store_databases),
                    limitations=features.limitations(),
                )
        except Exception as exc:
            entry.update(ok=False, error=str(exc))
        report.append(entry)
    return report


def collector_plan(config: AppConfig, tier: str) -> list[Collector]:
    """Which collectors a tier would run -- used by the ``collectors`` command."""
    return collectors_for(tier)
