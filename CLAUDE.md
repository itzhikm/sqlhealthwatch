# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Orientation

This is a **collector only**. It samples DMVs from a fleet of SQL Server instances into the
`DBA_Monitoring` repository and raises threshold alerts. There is no report, no Excel/CSV
deliverable and no web front end — do not add one without being asked. A DBA reads the `mon` tables.

`sqlhealthwatch_spec.md` is the design document, with a **scope amendment at the top** recording
this narrowing: §10 (Reporting) and §10.3 (Excel/CSV export) are out of scope; everything else in it
stands. Read the relevant section before changing behaviour.

Phase 7 of §17 (`--apply` remediation) is also out of scope: v1 recommends, never acts. Index and
statistics findings are stored, not applied.

## Commands

```bash
pytest                                       # unit suite: no database, no ODBC driver required
pytest tests/test_derive.py::TestPleFloor -v # a single test
SHW_TEST_CONFIG=config pytest -m integration # live-instance tests, deselected by default

python -m sqlhealthwatch                     # fast tier (the default with no args)
python -m sqlhealthwatch fast | daily [--server NAME] [--dry-run]
python -m sqlhealthwatch test-conn [--server NAME] [--repo]
python -m sqlhealthwatch prune [--maintain-indexes]
python -m sqlhealthwatch collectors          # tier → collector → table map
```

`src/sqlhealthwatch/__main__.py` is the single entry point (argparse, no CLI framework). The
installed `sqlhealthwatch` console script points at the same `main()`. Exit codes are load-bearing —
Task Scheduler shows them: `0` ok (including partial failures and a skipped run), `1` run failed or
every server failed, `2` bad config.

Install for development with `pip install -e ".[dev]"`. Tests add `src/` to `sys.path` via
`tests/conftest.py`, so they also run uninstalled.

## Architecture invariants

- **A collector is self-contained.** Each `collectors/*.py` owns its T-SQL file, its `mon` table, its
  transform, and the thresholds it feeds. Adding a metric = one collector module + one table in
  `storage/schema.sql` + a threshold entry + one line in `collectors/__init__.py`. Contract in
  `collectors/base.py`: `name`, `tier`, `table`, `sql_file`, `applies_to(ctx)`, `sql_file_for(ctx)`,
  `transform(rows, ctx)`. `PerDatabaseCollector` iterates user databases and skips a failing one
  rather than losing the server.
- **Every collector persists to a table.** There are no report-only collectors any more — data
  collected and discarded is a query run against production for nothing.
- **SQL lives in `sql/*.sql`, never inline in Python** — a DBA must be able to read, diff and tune
  the queries without touching code.
- **Version variants are sibling files, not `if` branches inside a query** (`space_drive.sql` /
  `space_drive_legacy.sql`). Only `applies_to` and `sql_file_for` may branch on version.
- **Nothing is assumed present.** `version.py` probes object existence (`OBJECT_ID(...)` in
  `feature_probe.sql`), because service-pack level — not major version — gates
  `dm_db_stats_properties` and `dm_os_volume_stats`. Never use `SERVERPROPERTY('ProductMajorVersion')`:
  it is NULL before 2014 SP2, which is exactly the fleet this must work on.
- **A missing feature degrades and is recorded; it never errors the run and is never silently
  filled.** The unavailable column stays NULL, `ServerFeatures.flags_json()` lands on
  `mon.server.feature_flags` so a query can tell which path produced a number, and
  `ServerContext.note()` records it in the run log.
- **Rates are derived, not collected.** Most DMVs are cumulative since restart. `analyze/derive.py`
  computes interval latency/throughput from consecutive samples; a counter moving backwards means a
  restart and returns `None`, never a negative. Zero IO in a window is `None`, not `0 ms`.
- **Threshold precedence is default → tag → server**, merged per category so overriding one IO
  threshold keeps the rest (`config.Thresholds.effective`). Findings carry a stable `fingerprint`
  (`server|category|metric|object`) that the router dedups on.
- **`mon.query_top` has two writers, by design.** `QueryHistoryCollector` owns duration/reads/exec on
  every instance and CPU only on the Query Store path; `CpuTopQueriesCollector` owns the plan-cache
  CPU ranking (which has database attribution). Changing one without the other double-counts.
- **Per-server isolation.** `runner.py` fans out over a bounded `ThreadPoolExecutor`; each worker owns
  its own instance *and* repository connections (pyodbc is not thread-safe). A failure is recorded in
  `mon.server_status`, raises a crit availability finding, and never aborts the run. A thread cannot
  be killed in Python, so the time budget is enforced by cursor timeouts plus a deadline check
  between collectors.
- **Repository is the source of truth**; `data/exports/` Parquet is an optional cold archive whose
  failure never fails a run.

## Production-safety rules (non-negotiable — this reads live prod)

- Every query against a monitored instance is read-only, bounded, and short. Fragmentation uses
  `LIMITED`, never `DETAILED`. `connection.py` sets a low `LOCK_TIMEOUT` and READ UNCOMMITTED on
  every session so the collector can never block production. `tests/test_sql_files.py` enforces both,
  and also that no `sql/*.sql` file is empty — a truncated query file would otherwise fail silently.
- Short `connect_timeout` (~5s) and `query_timeout` (~30s); one reconnect retry, then mark failed.
- Monitoring principal: `VIEW SERVER STATE`, `VIEW ANY DEFINITION`, `CONNECT`, `db_datareader`.
  **Never sysadmin.** The repo write login is a separate grant on the repository instance only.
- The repository must not live on a monitored production box.
- Secrets are never inline in YAML — `password_ref` uses `env:` / `credman:` / `dpapi:`, resolved at
  load. Connection strings pass through `connection.redact()` before any log.

## Storage conventions (`mon` schema)

Every metric row carries `server_id`, `run_id` and `collected_at_utc`. Sample tables use a
**nonclustered** `BIGINT IDENTITY` PK plus a **clustered index on `(collected_at_utc, server_id)`**,
so 7-day reads and retention deletes are range scans. Writes are append-only, batched via
`fast_executemany`; concurrent writers are expected.

`schema.sql` is applied idempotently on bootstrap — every DDL batch must stay guarded
(`tests/test_storage.py` fails an unguarded one). The literal `{schema}` is substituted at apply time.

Retention: 7 days raw, in batched `DELETE TOP (N)` loops. A new table must be added to `RAW_TABLES`
in `storage/repository.py` or it will never be pruned. Exceptions: `deadlock_event` 90 days,
`runs`/`server_status`/`alert_log` 30 days, `server`/`collector_watermark` permanent.

## Alerting

Only `warn`/`crit` in the fast-tier categories (cpu, memory, io, space, blocking, deadlock,
availability) are pushed. Index, statistics and query findings are written to `mon.findings` and left
there — they are not paged. Dedup is by fingerprint within the cooldown window; quiet hours suppress
everything except crit when `allow_crit` is set. A cooldown lookup failure fails *open*: a missed
page is worse than a duplicate one.

## Interpretation caveats to preserve in code

Plan cache is volatile, so pre-2016 query history is snapshot-to-snapshot only (`query_top.source`
says which). The missing-index DMV over-recommends and ignores write cost and overlap. PLE 300 is a
myth; `memory_sample.ple_dynamic_floor` is `(target GB / 4) × 300`, and per-NUMA-node PLE is kept in
`min_node_ple`. `index_usage_stats` resets on restart (and on rebuild in some builds), so "unused" is
only readable against `instance_meta.uptime_minutes`. Deadlocks are events, not states — they come
from system_health after the fact, never from a poll.

## Testing conventions

Collector logic is tested through `transform` with captured DMV result sets — no database. Each
version-gated collector is exercised against both `modern_features` and `legacy_features` (fixtures
in `conftest.py`) and asserted on which variant it chose and what it did with the reduced data.
`tests/test_runner.py` covers failure isolation and the overlap guard with fakes;
`tests/test_main.py` covers entry-point dispatch and exit codes.

## Open questions the spec flags (§19)

Which instance hosts `DBA_Monitoring`; the fleet's lowest SQL version and SP levels (decides whether
the 2005-era fallbacks are needed at all — they are currently gated off, not built); whether any
Azure SQL DB/MI instances exist; the alert channel; and whether top-query statement text may be
captured or must be hashed (`collection.statement_text_mode`). If a task depends on one of these, ask
rather than assuming.
