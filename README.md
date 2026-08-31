# sqlhealthwatch

A health **collector** for a fleet of production SQL Server instances, run from a single central
host. It samples six objectives — CPU, memory, disk, indexes, statistics and query history — into a
central SQL Server repository, and raises threshold alerts.

There is no report, no web front end and no dashboard. The collector's output is the
`DBA_Monitoring` database: query the `mon` tables directly, or point any BI/query tool at them.

Built from `sqlhealthwatch_spec.md` (see the scope amendment at the top of it).

**v1 recommends; it does not act.** No automated rebuilds, no automated statistics updates, no
killing sessions. Index and statistics findings are *stored* for a DBA to act on.

---

## What it does

```
40x SQL Server ──TDS/1433──▶ Collector host (Windows)
   DMVs, Query Store          ├─ fast tier   every 15 min   cpu, memory, io, waits, blocking, drives
                              └─ daily tier  off-hours      indexes, stats, queries, deadlocks, space
                                     │
                          ┌──────────┴──────────┐
                          ▼                     ▼
              DBA_Monitoring (mon schema)   Threshold alerts
                  7-day raw retention       (email / Teams / Slack)
                          │
                          └─▶ optional Parquet archive (data/exports/)
```

## Requirements

- Python 3.11+ on the collector host
- [Microsoft ODBC Driver 18 for SQL Server](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server)
- Windows is recommended so AD-integrated connections work natively. Linux works for a
  SQL-login-only fleet, or with Kerberos.
- A repository instance to host `DBA_Monitoring` — **not** one of the monitored production boxes.

## Install

```bash
pip install .
```

## Setup

**1. Provision the repository** on the chosen (dedicated, non-critical) instance:

```sql
-- sql/repository/create_database.sql      creates DBA_Monitoring, SIMPLE recovery
-- sql/repository/create_repo_login.sql    the collector's write login
```

**2. Provision each monitored instance** with the least-privilege read login:

```sql
-- sql/repository/create_monitor_login.sql
-- VIEW SERVER STATE + VIEW ANY DEFINITION + db_datareader. No sysadmin.
```

**3. Configure.** Four YAML files in `config/`, validated on load:

| File | Holds |
|---|---|
| `servers.yml` | the fleet inventory: host, auth mode, tags, enabled |
| `thresholds.yml` | fleet defaults plus per-tag and per-server overrides |
| `settings.yml` | repository, paths, retention, concurrency, tiers, collection |
| `alerts.yml` | channels, routing, cooldown, quiet hours |

Passwords are never inlined. A server using SQL auth carries a reference:

```yaml
  - name: PRD-SQL-02
    host: prd-sql-02.corp.local
    auth: sql
    username: svc_dba_monitor
    password_ref: env:PRD_SQL_02_PW    # or credman:TARGET / dpapi:PATH
```

Copy `.env.example` to `.env` (gitignored) and fill in the values.

**4. Verify:**

```bash
python -m sqlhealthwatch test-conn --repo      # repository write access + schema
python -m sqlhealthwatch test-conn             # every instance, with its feature report
```

`test-conn` prints what each instance can and cannot do — the "limited:" lines are the degraded
paths that instance will run on.

## Running the collector

The collector is a main module:

```bash
python -m sqlhealthwatch                  # fast tier (the default, so Run in an IDE works)
python -m sqlhealthwatch fast
python -m sqlhealthwatch daily
python -m sqlhealthwatch test-conn [--server NAME] [--repo]
python -m sqlhealthwatch prune [--maintain-indexes]
python -m sqlhealthwatch collectors       # tier → collector → table map
```

Options: `-c/--config DIR`, `-s/--server NAME`, `--dry-run` (collect and evaluate thresholds, write
nothing, send nothing).

`pip install .` also puts a `sqlhealthwatch` console script on PATH, which is the same entry point.

Exit codes: `0` success (including a run where some servers failed, and a run skipped by the overlap
guard), `1` the run failed or every server failed, `2` bad configuration. Task Scheduler's "last
result" column reads these.

## Scheduling

Two Windows Task Scheduler jobs, not a long-lived process — they survive reboots and surface
last-run status in a tool operations already uses:

| Job | Frequency | Command |
|---|---|---|
| fast tier | every 15 min | `python -m sqlhealthwatch fast` |
| daily tier | once, off-hours (06:00) | `python -m sqlhealthwatch daily` |
| retention | daily or weekly | `python -m sqlhealthwatch prune` |

A run takes a per-tier application lock on the repository. If the previous run of the same tier is
still going, the new one logs and skips rather than piling a second fan-out onto the fleet.

## Reading the data

Everything lands in the `mon` schema, keyed on `server_id`, `run_id` and `collected_at_utc`.

| Objective | Tables |
|---|---|
| CPU | `cpu_sample`, `query_top` (`rank_metric='cpu'`) |
| Memory | `memory_sample`, `memory_clerk` |
| Disk | `io_file_sample`, `space_db_sample`, `space_drive_sample`, `tempdb_sample` |
| Indexes | `index_frag`, `index_missing`, `index_unused`, `index_column` |
| Statistics | `stats_stale` |
| Query history | `query_top` (`source` = `query_store` \| `plan_cache`) |
| Contention | `wait_sample`, `blocking_event`, `deadlock_event` |
| Evaluation | `findings`, `alert_log` |
| Inventory / bookkeeping | `server`, `runs`, `server_status`, `collector_watermark` |

`mon.server.feature_flags` carries the per-instance capability JSON, so a query can tell whether a
given server's numbers came from the primary or the fallback path — see below.

## Onboarding a server

Add a block to `servers.yml`, run `create_monitor_login.sql` on the instance, then
`python -m sqlhealthwatch test-conn --server NAME`. No code change.

## Mixed versions are first-class

Pre-2016 instances are expected, not an edge case. The practical floor is SQL Server 2008 R2.

At connect time each instance is probed for **object existence**, not just version number — because
service-pack level, not major version, decides whether `sys.dm_db_stats_properties` (2008 R2 SP2 /
2012 SP1) and `sys.dm_os_volume_stats` (2008 R2 SP1) exist. Each collector then picks a sibling SQL
file; nothing branches inside a query.

| Objective | Primary | Fallback | What is lost |
|---|---|---|---|
| Drive free space | `sys.dm_os_volume_stats` | `xp_fixeddrives` | free MB only — no total, no %; alerting switches to absolute MB |
| Statistics | `sys.dm_db_stats_properties` | `STATS_DATE()` + `rowmodctr` | modification counts become per-table estimates (`stats_stale.is_estimate = 1`); dates stay exact |
| Query history | Query Store | plan cache | no durable history — cleared on restart, memory pressure, recompile (`query_top.source`) |
| Per-NUMA-node PLE | `Buffer Node` counters | instance-level PLE | a starved node hides in the aggregate |
| Deadlocks | system_health event_file | ring buffer | capped and in-memory, so older events can be missed |
| Query identity | `query_hash` (2008+) | `sql_handle` + offset | coarser identity across recompiles |

A degraded path is never silently blank: the columns it cannot fill stay NULL, the flag that explains
why is on `mon.server.feature_flags`, and the run log records it.

## Reading the numbers

Three caveats are built into the code, because ignoring them produces confident wrong conclusions:

- **Almost every DMV is cumulative since restart.** Interval rates (IO latency, throughput) are
  derived from consecutive samples and stored in the `interval_*` columns; a counter that moves
  backwards means a restart and is stored as NULL, not as a negative. Uptime is in
  `mon.instance_meta`. A NULL interval means *no IO happened in the window*, not zero latency.
- **PLE 300 is a myth.** `memory_sample.ple_dynamic_floor` is scaled from the buffer pool:
  `(target GB / 4) × 300`. Compare `page_life_expectancy` against that, not against 300.
- **The missing-index DMV over-recommends.** `index_missing` rows ignore write cost and do not know
  about existing indexes. Rank by `improvement_measure`, review against `index_column` for the same
  table, and never apply as collected.

## Building a standalone exe

Only needed for a collector host with no Python installed. Otherwise `pip install .` already gives
you a `sqlhealthwatch.exe` launcher.

```bash
pip install -r requirements-build.txt
pyinstaller packaging/sqlhealthwatch.spec --clean --noconfirm
# -> dist/sqlhealthwatch/  (~183 MB; pandas and pyarrow dominate)
```

Deploy `config/` and `sql/` *beside* the exe, not inside it — onboarding a server is a YAML edit and
tuning a query is a `.sql` edit, and neither should need a rebuild:

```
C:\sqlhealthwatch    sqlhealthwatch.exe
    _internal\      runtime (never separate this from the exe)
    config\  sql\   editable
    logs\           created on first run
```

Task Scheduler action: `C:\sqlhealthwatch\sqlhealthwatch.exe` with arguments
`daily --config C:\sqlhealthwatch\config`. Pass the config path absolutely so the job's working
directory cannot break it.

**The ODBC driver is not bundled and cannot be.** `pyodbc` is only the Python binding; Microsoft ODBC
Driver 18 is a Windows system component. A frozen exe still needs it installed on every collector
host — without it every connection fails with `IM002 ... no default driver specified`.

## Development

```bash
pip install -e ".[dev]"
pytest                      # unit suite: no database, no ODBC driver needed
pytest tests/test_derive.py::TestPleFloor -v    # a single test
```

Integration tests need a live instance and are deselected by default:

```bash
SHW_TEST_CONFIG=config pytest -m integration
```

The most useful one, `TestQueryMatrix`, parses every query — primary *and* legacy variant — against
the target instance without executing it. Point it at one instance per major version in the fleet to
prove the queries compile on the oldest box you actually own.

## Layout

```
config/                 the four YAML files
sql/                    the DMV queries, one file per collector, version variants side by side
  repository/           provisioning scripts
src/sqlhealthwatch/
  __main__.py           the entry point
  config.py             pydantic models + loader
  connection.py         connection factory, retry, session settings
  runner.py             tier orchestration, fan-out, failure isolation
  version.py            feature probing and gating
  collectors/           one module per metric; each owns its SQL, table and transform
  storage/              repository, schema.sql, retention, Parquet export
  analyze/              derived rates and threshold evaluation
  alerting/             router + email/Teams/Slack channels
  util/                 logging, time, secrets, statement text
tests/
```

Queries live in `sql/*.sql` rather than in Python strings so a DBA can read, diff and tune them
without touching code.

## Adding a metric

One collector module, one table in `storage/schema.sql`, one threshold entry, and one line in
`collectors/__init__.py`. Nothing else changes.

## Security

- Least privilege on the monitored instances: `VIEW SERVER STATE`, `VIEW ANY DEFINITION`, `CONNECT`,
  `db_datareader`. Never sysadmin.
- The repository write login is a separate grant on the repository instance only.
- Secrets are resolved from `env:` / `credman:` / `dpapi:` references at load; `.env` is gitignored,
  and connection strings are redacted before they reach a log.
- Every query against a monitored instance is read-only, bounded by a short timeout, and runs under
  a low `LOCK_TIMEOUT` so the collector can never block production. Index fragmentation uses
  `LIMITED` scans, never `DETAILED`.
- No data content is collected — metadata, counters, and the statement text of top queries. Set
  `collection.statement_text_mode: hash` if literals in that text are a concern.

## Not in scope

Reports, dashboards and any web front end. Automated remediation (`--apply`). Real-time streaming.
AG failover orchestration. Because the repository is a plain SQL Server database, a BI tool or
Grafana's MSSQL datasource can be pointed at it with no schema change if a dashboard is ever wanted.
