# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state

**This repository contains no code yet — only `sqlhealthwatch_spec.md` (1300 lines, v1.0).** The spec is
"ready to build from" and is the authoritative design document: layout, table schemas, DMV queries per
collector, thresholds, and phased build order all live there. Read the relevant spec section before
implementing anything; do not invent a different structure.

Build order the spec prescribes (§17): 1) config + connection + `test-conn` + version detection +
repository bootstrap → 2) fast-tier collectors → 3) daily-tier collectors → 4) analyzer + alerter →
5) report + export → 6) retention + scheduling. An `--apply` remediation mode is explicitly **out of v1
scope** — v1 recommends, never acts (no auto-rebuild, no auto-update-stats, no auto-kill).

## What this project is

A central Python collector on one Windows host that polls ~40 on-prem SQL Server instances over
pyodbc/TDS, writes samples to a central SQL Server repository database (`DBA_Monitoring`, `mon` schema),
evaluates thresholds into findings, and produces a daily HTML report + threshold alerts. The HTML report
*is* the dashboard — no Grafana, no TSDB, no real-time streaming.

## Commands (planned CLI, per spec §15)

```
sqlhealthwatch test-conn [--server NAME | --all | --repo]   # connectivity + perms + version/feature report
sqlhealthwatch run-fast                                     # 15-min tier
sqlhealthwatch run-daily                                    # off-hours heavy tier, then report/export/prune
sqlhealthwatch report [--date YYYY-MM-DD]
sqlhealthwatch export [--date YYYY-MM-DD]                   # xlsx + csv
sqlhealthwatch prune
```

Install: `pip install .` (Python 3.11+, MS ODBC Driver 18 required). Tests: `pytest`; a single test is
`pytest tests/test_x.py::test_name`. Integration tests requiring a live SQL Server must be gated/skipped
by default so the unit suite runs with no database.

## Architecture invariants

- **A collector is self-contained.** Each `collectors/*.py` owns its T-SQL file, its `mon` table, its
  transform, and the thresholds it feeds. Adding a metric = one collector module + one table + threshold
  entries, and *nothing else changes*. Contract (`collectors/base.py`):
  `name`, `tier` ("fast"|"daily"), `table`, `sql_file`/`sql_variants`, `applies_to(srv)`, `transform(rows, srv, run_id) -> DataFrame`.
- **SQL lives in `sql/*.sql`, never as inline Python strings.** A DBA must be able to read, diff, and tune
  the queries without touching Python.
- **Version variants are sibling files, not `if` branches inside a query** — e.g. `space_drive.sql` vs
  `space_drive_legacy.sql`, `stats_age.sql` vs `stats_age_legacy.sql`. `version.py` picks the file.
- **Nothing is assumed present.** Pre-2016 instances are first-class, not an edge case (floor: 2008 R2).
  `version.py` probes actual object availability per instance (`OBJECT_ID('sys.dm_db_stats_properties')`
  style, plus try-select) and caches it — SP level decides feature presence, so never trust a bare
  version→feature table. A missing feature degrades to the documented fallback in §2.1 or is skipped with
  a note; it must **never** error the run. Where fidelity is lost, the report shows a "limited on this
  version" badge (e.g. drive free MB with no %, plan-cache query history that isn't durable).
- **Rates are derived, not collected.** Most DMVs are cumulative-since-restart counters. CPU %, IO latency,
  and counter deltas are computed in `analyze/derive.py` from two consecutive samples, always interpreted
  against `sqlserver_start_time`.
- **Threshold precedence is default → tag → server** (`thresholds.yml` `overrides.by_tag` / `by_server`);
  findings carry a stable `fingerprint` (`server|category|metric|object`) that the alert router dedups on
  within a cooldown window.
- **Per-server isolation.** `runner.py` fans out over a bounded `ThreadPoolExecutor` (default 8 workers)
  with a per-server timeout; one slow or unreachable server is recorded in `mon.server_status` and raises a
  crit availability alert, but never aborts the run.
- **Repository is the source of truth**; Parquet/CSV under `data/exports/` are cold archive only.

## Production-safety rules (non-negotiable — this reads live prod)

- Every DMV query is read-only, bounded, and short-running. Index fragmentation uses `LIMITED`, never
  `DETAILED`. Low `SET LOCK_TIMEOUT` so the monitor never blocks production. No hints that force recompiles.
- Short `connect_timeout` (~5s) and `query_timeout` (~30s, longer only for daily index scans); one quick
  reconnect retry on transient errors, then mark failed.
- Monitoring principal needs only `VIEW SERVER STATE`, `VIEW ANY DEFINITION`, `CONNECT` (+ Query Store read).
  **Never require sysadmin.** The repo write principal is a separate grant on the repo instance only.
- The repository must **not** live on one of the 40 monitored prod boxes.
- Secrets are never inline in YAML — `password_ref` uses `env:` / `credman:` / `dpapi:` refs resolved at
  load; `.env` is gitignored.

## Storage conventions (`mon` schema)

Every metric row carries `server_id` (from the `mon.server` dimension), `run_id` (UNIQUEIDENTIFIER), and
`collected_at_utc`. Sample tables use a **nonclustered** `BIGINT IDENTITY` PK plus a **clustered index on
`(collected_at_utc, server_id)`** so 7-day-window queries and retention are range scans. Writes are
append-only, batched via `fast_executemany=True` (~5000 rows), one transaction per collector per server;
concurrent writers are expected and fine.

Retention: 7 days raw, pruned in batched `DELETE TOP (N)` loops so the log stays small. Exceptions —
`mon.deadlock_event` keeps 90 days, `mon.runs`/`mon.server_status` ~30 days, and `mon.server` /
`mon.collector_watermark` are permanent state.

## Interpretation caveats to preserve in code and report text (§18)

Plan cache is volatile (cleared on restart/memory pressure), so pre-2016 query history is
snapshot-to-snapshot only. The missing-index DMV over-recommends and ignores write cost and overlap — rank
it, never auto-apply. PLE 300 is a myth; scale by buffer pool and read per-NUMA-node. `index_usage_stats`
can reset on rebuild, so weigh uptime before calling an index unused. `xp_fixeddrives` is undocumented but
present everywhere and is an acceptable read-only fallback. Recommendations ship **generated but
non-executed** T-SQL for the DBA to copy.

## Open questions the spec flags (§19)

Which instance hosts `DBA_Monitoring`; the fleet's lowest SQL version and SP levels (decides how many
legacy SQL variants are actually needed); whether any Azure SQL DB/MI instances exist; the v1 alert channel
(email is the default); and whether capturing top-query statement text is acceptable or must be
param-stripped/hashed. If a task depends on one of these, ask rather than assuming.
