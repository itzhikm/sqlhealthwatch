# Production SQL Health Watch — Technical Specification

**Project codename:** `sqlhealthwatch`
**Author / owner:** Itzik Manhaimer (DBA)
**Version:** 1.0 (spec)
**Date:** 2026-08-29
**Status:** Design — ready to build from

---

## 1. Purpose & goals

Build a Python project that performs **daily (and intraday) health monitoring of ~40 production SQL Server instances** from a single central collector, surfaces system bottlenecks, and produces a report a DBA reviews each morning plus threshold-based alerts.

The system must answer, per server and across the fleet:

1. **CPU** — which instances are CPU-bound, top CPU-consuming queries, signal-wait pressure (scheduler contention).
2. **Memory pressure** — Page Life Expectancy, memory grants pending, buffer cache hit, memory-related waits, plan cache churn.
3. **Disk usage & throughput** — database/drive free space and growth, file-level read/write latency and IO stall, tempdb pressure.
4. **Index optimization** — fragmentation, missing-index recommendations, unused / duplicate / rarely-used indexes.
5. **Statistics** — stale statistics (age + modification counter), auto-update status.
6. **Query history** — top queries by duration, CPU, reads, and execution count, with intraday/day-over-day comparison (via Query Store where available, plan cache DMVs as fallback).

### 1.1 Non-goals (v1)

- No automated remediation (no auto-rebuild, no auto-update-stats, no auto-kill). v1 **recommends**; it does not act. A future `--apply` mode is called out but out of scope.
- No real-time streaming/APM. Sampling is tiered (15-min / daily), not sub-second.
- No Grafana / external TSDB. The **daily HTML report is the dashboard**. (Note: because the repository is now a central SQL Server DB, Grafana's built-in MSSQL datasource *could* be pointed at it later with no schema changes — called out as an easy future option, not built in v1.)
- No Availability Group failover orchestration; AG health is read-only reported only if present.

---

## 2. Scope & environment assumptions

| Item | Assumption (confirm before build) |
|---|---|
| Fleet size | ~40 SQL Server instances |
| Platform | Primarily on-prem **SQL Server on Windows**, **mixed versions** across the fleet |
| Version range | **Mixed — pre-2016 instances are expected and first-class**, not an edge case. Practical floor: **SQL Server 2008 R2** (2005/2008 RTM supported with the extra fallbacks noted in §2.1). Each instance is version-detected at connect and every collector self-gates: modern DMVs where present, documented legacy substitutes otherwise. The three version-sensitive areas are **Query Store** (2016+), **`sys.dm_db_stats_properties`** (2008 R2 SP2 / 2012 SP1+), and **`sys.dm_os_volume_stats`** (2008 R2 SP1+). Full matrix + fallbacks in **§2.1**. |
| Auth | **Mixed, per-instance**: Windows/AD integrated auth **or** SQL login. Config selects per server. |
| Topology | **Central collector** pulls from all 40 (outbound TCP 1433 or named-instance ports; network line-of-sight required). |
| Collector host | A single Windows host is recommended (so AD-integrated connections work natively). Linux is possible for SQL-login-only fleets, or Windows-auth via Kerberos — see §5.3. |
| Privilege | A dedicated monitoring principal on each instance with **VIEW SERVER STATE**, **VIEW ANY DEFINITION**, and read on Query Store / DMVs. No sysadmin required. See §11.1. |
| Storage | **Central SQL Server repository database** (e.g. `DBA_Monitoring`) as the operational store; Parquet/CSV kept only as cold export artifacts. **7-day raw retention.** |
| Repository host | A **dedicated / non-critical instance** should host the monitoring DB — **not** one of the 40 monitored prod boxes — so collection load never touches production and a prod-server outage never takes monitoring down. Confirm which instance. See §8.1. |
| Outputs | Daily HTML report (per-server + fleet rollup), threshold alerts (email / Teams / Slack — config-driven), CSV/Excel exports. |

> **Open confirmations** (do not block the build, but validate early):
> - **Which instance hosts the `DBA_Monitoring` repository?** Recommend a dedicated/non-critical instance separate from the 40 monitored boxes (§8.1).
> - Are any instances **Azure SQL DB / Managed Instance**? (DMV surface differs — some server-scoped DMVs are unavailable on Azure SQL DB.) Spec assumes **box product** only.
> - **What is the lowest version actually in the fleet** (2008 R2? 2012? 2014?), and the service-pack levels on the pre-2012 boxes? SP level decides whether `dm_db_stats_properties` / `dm_os_volume_stats` are present (§2.1). Any 2005/2008-RTM boxes need the oldest fallbacks.
> - Preferred single alert channel for v1 (email is the safe default; Teams/Slack via webhook).

### 2.1 Version support & feature gating

The collector calls `version.py` once per instance at connect time, resolves a feature set from `ProductMajorVersion` + `ProductLevel`/build, and each collector's `applies_to()` / SQL-variant selection keys off it. Nothing is assumed present; missing features degrade to a documented substitute (or are skipped with a note on the server's report page), never error the run.

**Feature → minimum version → fallback matrix:**

| Objective / collector | Primary DMV | Min version for primary | Pre-min fallback | Fidelity of fallback |
|---|---|---|---|---|
| CPU % (ring buffer) | `sys.dm_os_ring_buffers` (SCHEDULER_MONITOR) | 2008+ (SystemHealth XML) | On 2005, ring-buffer XML lacks `ProcessUtilization`; use `sys.dm_os_schedulers` + perfmon `% Processor Time` | Slightly coarser |
| CPU signal-wait %, waits | `sys.dm_os_wait_stats` | 2005+ | — | Full |
| Memory (PLE, grants, counters) | `sys.dm_os_performance_counters` | 2005+ | — | Full |
| Per-NUMA-node PLE | `Buffer Node` object | 2008+ | Fall back to instance-level `Buffer Manager` PLE | Loses per-node detail |
| IO latency / throughput | `sys.dm_io_virtual_file_stats()` | 2005+ | — | Full |
| **Drive free space** | `sys.dm_os_volume_stats()` | **2008 R2 SP1 / 2012+** | `xp_fixeddrives` (free MB per drive) | **Free MB only — no total, no free %**; `drive_free_pct` alerts disabled, absolute-MB threshold used instead |
| DB file space | `sys.database_files` + `FILEPROPERTY` | 2005+ | — | Full |
| Index fragmentation | `sys.dm_db_index_physical_stats(...,'LIMITED')` | 2005+ | — | Full |
| Missing indexes | `sys.dm_db_missing_index_*` | 2005+ | — | Full |
| Unused/duplicate indexes | `sys.dm_db_index_usage_stats` | 2005+ | — | Full (mind counter reset on restart/rebuild) |
| **Statistics age + mod counter** | `sys.dm_db_stats_properties()` | **2008 R2 SP2 / 2012 SP1+** | `STATS_DATE()` for last-updated + `sys.sysindexes.rowmodctr` (or `DBCC SHOW_STATISTICS ... WITH STAT_HEADER`) for modifications | Age is exact; `rowmodctr` is per-table (not per-stat) and deprecated — mod-ratio is approximate |
| **Query history (durations/occurrences)** | **Query Store** (`sys.query_store_*`) | **2016+ & QS enabled per DB** | `sys.dm_exec_query_stats` (plan cache) snapshotted daily into `mon.query_top`, diffed by `query_hash` | **No durable history** — plan cache clears on restart/memory pressure/recompile; day-over-day is snapshot-to-snapshot only, `query_hash` identity weaker than QS `query_id` |
| Instance uptime | `sys.dm_os_sys_info.sqlserver_start_time` | 2008+ | On 2005, use `login_time` of SPID 1 in `sys.dm_exec_sessions` | Equivalent |
| `query_hash` / `query_plan_hash` | columns on `dm_exec_query_stats` | 2008+ | On 2005, group by `sql_handle`+offsets only | Coarser query identity |

**Build notes baked into the design:**
- Version-specific SQL lives as sibling files in `sql/` (e.g. `space_drive.sql` vs `space_drive_legacy.sql`, `stats_age.sql` vs `stats_age_legacy.sql`), chosen by `version.py` — no branching inside a single query.
- `ProductLevel` (SP) matters, not just major version: a 2008 R2 box can be **RTM/SP1/SP2**, and that decides whether `dm_db_stats_properties` (SP2) and `dm_os_volume_stats` (SP1) exist. The collector probes actual object availability (a cheap `OBJECT_ID('sys.dm_db_stats_properties')` / try-select) and caches the result per instance, so it's correct even on odd patch levels rather than trusting a version→feature table blindly.
- Where a fallback loses fidelity, the per-server report page shows a small **"limited on this version"** badge (e.g. "drive free % unavailable — showing free MB"; "query history from plan cache — not durable") so a reading is never silently misinterpreted.
- **Practical floor is 2008 R2.** If 2005 (or 2000) instances exist, flag them — the extra 2005 fallbacks above are cheap to add but should be confirmed as actually needed before building them.

---

## 3. High-level architecture

```
                        ┌──────────────────────────────────────────────┐
                        │             Central Collector Host            │
                        │                (Windows)                      │
                        │                                               │
  40x SQL Server  ◄─────┤  Scheduler (APScheduler / Task Scheduler)     │
  (DMVs, Query    │     │        │                                      │
   Store)         │     │        ├── Fast tier  (every 15 min)          │
                  │     │        │     └─ collectors: cpu, memory, io,   │
                  ├─────┤        │        waits, blocking, space        │
   TDS/1433       │     │        └── Daily tier (off-hours, e.g. 06:00) │
   pyodbc /       │     │              └─ collectors: index, stats,     │
   AD or SQL auth │     │                 querystore, config, bigspace  │
                  │     │        │                                      │
                  │     │   Per-server workers (thread/async pool,      │
                  │     │   bounded concurrency, per-server timeout)    │
                  │     │        │                                      │
                  │     │        ▼                                      │
                  │     │   Normalize → validate → write                │
                  │     │        │                                      │
                  │     │        ▼                                      │
                  │     │   ┌──────────────────┐ ┌─────────────────┐   │
                  │     │   │ Central SQL Srv  │ │ Parquet/CSV     │   │
                  │     │   │ DBA_Monitoring   │ │ (cold exports)  │   │
                  │     │   │ 7-day raw        │ └─────────────────┘   │
                  │     │   └──────┬───────────┘                       │
                  │     │          │                                    │
                  │     │   ┌──────┴───────┐   ┌───────────────────┐    │
                  │     │   │ Analyzer /   │→  │ Alerter (email/   │    │
                  │     │   │ thresholds   │   │ Teams/Slack)      │    │
                  │     │   └──────┬───────┘   └───────────────────┘    │
                  │     │          ▼                                    │
                  │     │   ┌──────────────┐                           │
                  │     │   │ HTML report  │  (per-server + rollup)    │
                  │     │   └──────────────┘                           │
                  └─────┴───────────────────────────────────────────────┘
```

**Data flow:** *Scheduler → per-server Collector (runs a set of DMV queries) → Normalizer → Storage (central SQL Server `DBA_Monitoring` raw + Parquet export) → Analyzer (thresholds + trend/derivation) → Alerter + HTML Report + CSV/Excel export → Retention pruner.*

Each collector is a self-contained module that owns: (a) the T-SQL it runs, (b) the target table, (c) the transform, (d) the thresholds it feeds. Adding a new metric = adding one collector module + one table + threshold entries. No other module changes.

---

## 4. Technology stack

| Concern | Choice | Notes |
|---|---|---|
| Language | Python 3.11+ | type hints, `dataclasses`/`pydantic` models |
| DB driver | **pyodbc** + Microsoft ODBC Driver 18 for SQL Server | supports both `Trusted_Connection=yes` (AD) and SQL login; `Encrypt=yes;TrustServerCertificate=...` config-driven. `pymssql` is a documented fallback for pure Linux/SQL-login. |
| Scheduling | **APScheduler** (in-process) for dev; **Windows Task Scheduler** invoking CLI entrypoints for prod | two entrypoints: `run-fast`, `run-daily`. Prod prefers OS scheduler for resiliency. |
| Data handling | **pandas** + **pyarrow** | DataFrame per collector; Parquet export |
| Operational store | **Central SQL Server database** (`DBA_Monitoring`), written via pyodbc with `fast_executemany=True` (optionally SQLAlchemy Core for schema mgmt) | concurrent writers OK — no single-writer bottleneck; see §6, §8 |
| Repository access | Same **ODBC Driver 18** used for the monitored fleet; a separate connection block for the repository | bulk insert via `fast_executemany` or table-valued parameters |
| Config | **YAML** (`servers.yml`, `thresholds.yml`, `settings.yml`) + `.env` for secrets | validated by pydantic on load |
| Secrets | Windows Credential Manager / DPAPI or env vars; never plaintext in YAML | see §11.2 |
| Templating | **Jinja2** for HTML report | self-contained HTML, inline CSS, embedded charts |
| Charts in report | inline SVG / small JS (Chart.js embedded) or pre-rendered matplotlib PNGs (base64) | no external CDN dependency required |
| Excel export | **openpyxl** | per-run workbook |
| Concurrency | `concurrent.futures.ThreadPoolExecutor` (DB IO is blocking) with bounded pool (e.g. 8–10) | per-server timeout; failures isolated |
| Logging | `logging` + rotating file handler, structured (JSON option) | per-run run_id |
| CLI | **typer** or argparse | `sqlhealthwatch run-fast|run-daily|report|export|test-conn|prune` |
| Tests | **pytest** | unit (transforms/thresholds) + integration (against a local/dev SQL Server or container) |
| Packaging | `pyproject.toml`, installable console script | optional Docker for Linux collector |

---

## 5. Project layout

```
sqlhealthwatch/
├── pyproject.toml
├── README.md
├── config/
│   ├── settings.yml          # global: paths, retention, concurrency, tiers
│   ├── servers.yml           # 40 instances: host, port, auth mode, tags, enabled
│   ├── thresholds.yml        # metric thresholds (warn/crit) w/ per-server overrides
│   └── alerts.yml            # channels: email/teams/slack, routing, quiet hours
├── .env                      # secrets (gitignored)
├── src/sqlhealthwatch/
│   ├── __init__.py
│   ├── cli.py                # entrypoints: run-fast, run-daily, report, export, test-conn, prune
│   ├── config.py             # pydantic models + loader/validation
│   ├── connection.py         # connection factory (AD vs SQL login), retry, timeout
│   ├── runner.py             # orchestrates a tier: fan-out over servers, collect run
│   ├── version.py            # detect SQL version/edition; feature gating (Query Store etc.)
│   ├── collectors/
│   │   ├── base.py           # Collector ABC: sql, tier, transform, table, applies_to()
│   │   ├── cpu.py
│   │   ├── memory.py
│   │   ├── io_disk.py
│   │   ├── waits.py
│   │   ├── blocking.py
│   │   ├── deadlocks.py       # system_health XE deadlock graphs (event, not poll)
│   │   ├── space.py
│   │   ├── indexes.py
│   │   ├── statistics.py
│   │   ├── query_store.py    # + plan_cache.py fallback
│   │   └── instance_meta.py  # version, uptime, config, wait-reset baseline
│   ├── storage/
│   │   ├── repository.py      # SQL Server repo: connection, bulk insert (fast_executemany), schema bootstrap
│   │   ├── schema.sql         # DBA_Monitoring DDL (tables, indexes, optional partitioning)
│   │   ├── parquet_export.py
│   │   └── retention.py       # 7-day prune (DELETE / partition switch)
│   ├── analyze/
│   │   ├── thresholds.py      # evaluate metrics vs thresholds → findings
│   │   ├── derive.py          # rate calcs (IO latency, CPU %, deltas between samples)
│   │   └── recommendations.py # index/stats/query recommendations text
│   ├── alerting/
│   │   ├── router.py          # dedup, severity, quiet hours, per-server routing
│   │   ├── email.py
│   │   ├── teams.py
│   │   └── slack.py
│   ├── report/
│   │   ├── html.py            # Jinja2 render, fleet rollup + per-server pages
│   │   ├── excel.py
│   │   └── templates/
│   │       ├── fleet.html.j2
│   │       └── server.html.j2
│   └── util/
│       ├── logging.py
│       └── timeutil.py
├── sql/                       # the DMV queries, one .sql per collector (version variants)
│   ├── cpu_ring_buffer.sql
│   ├── cpu_top_queries.sql
│   ├── memory_clerks.sql
│   ├── perf_counters.sql
│   ├── io_file_stats.sql
│   ├── waits.sql
│   ├── blocking.sql
│   ├── deadlocks.sql          # + deadlocks_ringbuffer.sql / deadlocks_2008.sql variants
│   ├── space_db.sql
│   ├── space_drive.sql
│   ├── space_drive_legacy.sql # xp_fixeddrives (pre-2008 R2 SP1: no volume_stats)
│   ├── index_frag.sql
│   ├── index_missing.sql
│   ├── index_usage.sql
│   ├── stats_age.sql
│   ├── stats_age_legacy.sql   # STATS_DATE + rowmodctr (pre-2008R2 SP2/2012 SP1)
│   ├── feature_probe.sql      # per-instance version + object-availability probe
│   ├── querystore_top.sql
│   ├── plan_cache_top.sql     # pre-2016 / QS-off: plan-cache query history
│   └── repository/            # repository-side scripts
│       ├── create_database.sql       # CREATE DATABASE DBA_Monitoring + schema mon
│       └── create_repo_login.sql     # collector login: db_datawriter/db_datareader + ddl bootstrap
├── data/
│   └── exports/YYYY-MM-DD/    # parquet + csv + xlsx per run/day (cold archive; repo is source of truth)
├── reports/YYYY-MM-DD/        # rendered HTML
└── tests/
```

Keeping SQL in `sql/*.sql` (not inline strings) lets a DBA read, diff, and tune the queries without touching Python, and lets version variants live side by side.

---

## 6. Configuration model

### 6.1 `servers.yml`

```yaml
defaults:
  driver: "ODBC Driver 18 for SQL Server"
  encrypt: true
  trust_server_certificate: true   # set false once certs are trusted
  connect_timeout_s: 5
  query_timeout_s: 30
  tags: []

servers:
  - name: PRD-SQL-01
    host: prd-sql-01.corp.local
    port: 1433
    auth: windows            # windows | sql
    enabled: true
    tags: [tier1, erp, us-east]
  - name: PRD-SQL-02
    host: prd-sql-02.corp.local
    instance: SQL2019         # named instance (resolved via SQL Browser or explicit port)
    auth: sql
    username: svc_dba_monitor
    password_ref: env:PRD_SQL_02_PW   # never inline
    enabled: true
    tags: [tier2, reporting]
```

- `auth: windows` → connection string uses `Trusted_Connection=yes` (collector runs as the AD service account).
- `auth: sql` → `UID`/`PWD`, password pulled from the ref (`env:` / `credman:` / `dpapi:`), never stored in YAML.
- `tags` drive report grouping, threshold overrides, and alert routing.

### 6.2 `thresholds.yml`

```yaml
# fleet defaults; per-server or per-tag overrides allowed
defaults:
  cpu:
    sustained_pct_warn: 80        # avg over last N fast samples
    sustained_pct_crit: 90
    signal_wait_pct_warn: 25      # scheduler/CPU pressure
  memory:
    ple_warn: 300                 # seconds; scaled by buffer pool size (see §7.2)
    ple_crit: 180
    memory_grants_pending_warn: 1
  io:
    read_latency_ms_warn: 20
    read_latency_ms_crit: 50
    write_latency_ms_warn: 20
    write_latency_ms_crit: 50
  space:
    db_free_pct_warn: 15
    db_free_pct_crit: 8
    drive_free_pct_warn: 15
    drive_free_pct_crit: 8
  index:
    frag_pct_min_report: 15       # ignore below this
    frag_pct_rebuild: 30          # >30 rebuild, 15-30 reorg (guidance)
    min_page_count: 1000          # ignore tiny indexes
  stats:
    stale_days_warn: 7
    modification_ratio_warn: 0.20 # modified rows / rowcount
  blocking:
    block_seconds_warn: 30
    block_seconds_crit: 120
  deadlock:
    count_24h_warn: 1             # any deadlock in the last 24h is worth surfacing
    count_24h_crit: 10            # a burst = a hot code path to fix
overrides:
  by_tag:
    reporting:
      io: { read_latency_ms_warn: 40 }   # DW/reporting tolerates higher read latency
  by_server:
    PRD-SQL-01:
      cpu: { sustained_pct_warn: 85 }
```

### 6.3 `settings.yml`

```yaml
repository:                       # the central SQL Server monitoring database
  name: PRD-DBA-REPO              # instance that hosts DBA_Monitoring (NOT one of the 40 prod boxes)
  host: prd-dba-repo.corp.local
  port: 1433
  database: DBA_Monitoring
  schema: mon
  auth: windows                   # windows | sql  (windows preferred; else password_ref)
  # username: svc_dba_monitor
  # password_ref: env:REPO_PW
  encrypt: true
  trust_server_certificate: true
  bulk: { fast_executemany: true, batch_rows: 5000 }
paths:
  exports: data/exports           # cold Parquet/CSV/xlsx archive
  reports: reports
retention:
  raw_days: 7
  prune_strategy: delete          # delete | partition_switch (see §9)
  rebuild_repo_indexes: weekly    # keep the monitoring DB's own indexes healthy
concurrency:
  max_workers: 8
  per_server_timeout_s: 120
tiers:
  fast_minutes: 15
  daily_time: "06:00"      # local collector time; run off-hours
report:
  include_charts: true
  top_n_queries: 25
```

---

---

## 7. Collectors — objectives, DMV queries, metrics, thresholds

Every collector implements the `base.Collector` contract:

```python
class Collector(ABC):
    name: str
    tier: Literal["fast", "daily"]
    table: str
    sql_file: str                       # or sql_variants: dict[version -> file]
    def applies_to(self, srv: ServerInfo) -> bool: ...   # feature/version gate
    def transform(self, rows, srv, run_id) -> pd.DataFrame: ...
```

The collector runs its `.sql` against each server, tags every row with `server_name` + `run_id` + `collected_at_utc`, and returns a normalized DataFrame the storage layer writes. Rate-based metrics (CPU %, IO latency ms/IO, counter deltas) are computed in `analyze/derive.py` from **two consecutive samples**, because most DMVs are cumulative-since-restart counters.

> **DMV note:** All queries below are the box-product (on-prem) form. They require `VIEW SERVER STATE`. Column availability varies slightly by version; the build must validate against your lowest version. Queries are written to be safe/read-only and short-running.

---

### 7.1 CPU — `collectors/cpu.py` (fast tier + daily top-queries)

**Objective:** detect CPU-bound instances and identify the queries driving CPU.

**(a) Recent CPU utilization (SQL process vs other) — ring buffer, last ~256 min**

```sql
-- cpu_ring_buffer.sql : SQL Server process CPU % history from the scheduler ring buffer
DECLARE @ts_now BIGINT = (SELECT cpu_ticks/(cpu_ticks/ms_ticks) FROM sys.dm_os_sys_info);
SELECT TOP (15)
    DATEADD(ms, -1 * (@ts_now - [timestamp]), GETDATE()) AS event_time,
    record.value('(./Record/SchedulerMonitorEvent/SystemHealth/ProcessUtilization)[1]','int')      AS sql_cpu_pct,
    record.value('(./Record/SchedulerMonitorEvent/SystemHealth/SystemIdle)[1]','int')              AS system_idle_pct,
    100 - record.value('(./Record/SchedulerMonitorEvent/SystemHealth/SystemIdle)[1]','int')
        - record.value('(./Record/SchedulerMonitorEvent/SystemHealth/ProcessUtilization)[1]','int') AS other_process_pct
FROM (
    SELECT [timestamp], CONVERT(xml, record) AS record
    FROM sys.dm_os_ring_buffers
    WHERE ring_buffer_type = N'RING_BUFFER_SCHEDULER_MONITOR'
      AND record LIKE '%<SystemHealth>%'
) AS x
ORDER BY [timestamp] DESC;
```
Store the most recent sample as the point-in-time CPU %; the fast tier captures the trend.

**(b) Signal-wait ratio (scheduler/CPU pressure indicator)**

```sql
-- part of waits.sql, surfaced for CPU: high signal_wait% => threads ready but waiting for CPU
SELECT
    SUM(signal_wait_time_ms) * 100.0 / NULLIF(SUM(wait_time_ms),0) AS signal_wait_pct,
    SUM(wait_time_ms) AS total_wait_ms
FROM sys.dm_os_wait_stats
WHERE wait_type NOT IN (  -- benign/idle waits excluded
    'CLR_SEMAPHORE','LAZYWRITER_SLEEP','RESOURCE_QUEUE','SLEEP_TASK','SLEEP_SYSTEMTASK',
    'SQLTRACE_BUFFER_FLUSH','WAITFOR','LOGMGR_QUEUE','CHECKPOINT_QUEUE','REQUEST_FOR_DEADLOCK_SEARCH',
    'XE_TIMER_EVENT','BROKER_TO_FLUSH','BROKER_TASK_STOP','CLR_MANUAL_EVENT','CLR_AUTO_EVENT',
    'DISPATCHER_QUEUE_SEMAPHORE','FT_IFTS_SCHEDULER_IDLE_WAIT','XE_DISPATCHER_WAIT','XE_DISPATCHER_JOIN',
    'BROKER_EVENTHANDLER','TRACEWRITE','FT_IFTSHC_MUTEX','SQLTRACE_INCREMENTAL_FLUSH_SLEEP',
    'DIRTY_PAGE_POLL','SP_SERVER_DIAGNOSTICS_SLEEP','HADR_FILESTREAM_IOMGR_IOCOMPLETION',
    'ONDEMAND_TASK_QUEUE','BROKER_RECEIVE_WAITFOR','PWAIT_ALL_COMPONENTS_INITIALIZED');
```
Also capture **runnable tasks** and **scheduler count** for context:
```sql
SELECT COUNT(*) AS runnable_tasks_now
FROM sys.dm_os_schedulers WHERE status = 'VISIBLE ONLINE' AND is_online = 1;  -- + sum(runnable_tasks_count)
```

**(c) Top CPU-consuming queries — daily tier**

```sql
-- cpu_top_queries.sql : top statements by total worker (CPU) time from the plan cache
SELECT TOP (25)
    qs.total_worker_time                                   AS total_cpu_us,
    qs.execution_count,
    qs.total_worker_time / NULLIF(qs.execution_count,0)    AS avg_cpu_us,
    qs.total_elapsed_time / NULLIF(qs.execution_count,0)   AS avg_elapsed_us,
    qs.total_logical_reads / NULLIF(qs.execution_count,0)  AS avg_logical_reads,
    qs.last_execution_time,
    DB_NAME(CONVERT(int, pa.value))                        AS database_name,
    SUBSTRING(st.text, (qs.statement_start_offset/2)+1,
        ((CASE qs.statement_end_offset WHEN -1 THEN DATALENGTH(st.text)
              ELSE qs.statement_end_offset END - qs.statement_start_offset)/2)+1) AS statement_text,
    qs.query_hash, qs.query_plan_hash
FROM sys.dm_exec_query_stats qs
CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
OUTER APPLY sys.dm_exec_plan_attributes(qs.plan_handle) pa
WHERE pa.attribute = 'dbid'
ORDER BY qs.total_worker_time DESC;
```

| Metric | Source | Threshold key |
|---|---|---|
| `sql_cpu_pct` (latest + rolling avg) | ring buffer | `cpu.sustained_pct_warn/crit` |
| `signal_wait_pct` | wait_stats | `cpu.signal_wait_pct_warn` |
| `runnable_tasks_now` | schedulers | context |
| top-N queries by CPU | query_stats | report only (feeds recommendations) |

---

### 7.2 Memory pressure — `collectors/memory.py` (fast tier)

**Objective:** detect buffer-pool pressure, pending memory grants, and cache churn.

**(a) Key performance counters (PLE, grants pending, buffer cache hit, memory grants outstanding)**

```sql
-- perf_counters.sql : pull the memory-relevant counters in one shot
SELECT
    RTRIM(counter_name) AS counter_name,
    RTRIM(instance_name) AS instance_name,
    cntr_value
FROM sys.dm_os_performance_counters
WHERE (object_name LIKE '%Buffer Manager%' AND counter_name IN
        ('Page life expectancy','Buffer cache hit ratio','Buffer cache hit ratio base',
         'Free list stalls/sec','Lazy writes/sec','Page reads/sec','Page writes/sec'))
   OR (object_name LIKE '%Memory Manager%' AND counter_name IN
        ('Memory Grants Pending','Memory Grants Outstanding','Total Server Memory (KB)',
         'Target Server Memory (KB)'))
   OR (object_name LIKE '%Buffer Node%' AND counter_name = 'Page life expectancy');
```

> **PLE caveat:** the flat "300 seconds" rule is obsolete. Threshold scales with buffer pool size: **PLE floor ≈ (Target Server Memory GB / 4) × 300**. `derive.py` computes the per-server dynamic PLE floor from `Target Server Memory (KB)` and compares against it; `thresholds.yml` `ple_warn` is the absolute lower bound. On NUMA boxes, read **per-node PLE** (`Buffer Node`) — a low node PLE hides in the aggregate.

**(b) Memory clerks (where memory is going) — daily/context**

```sql
-- memory_clerks.sql : top memory consumers
SELECT TOP (10)
    [type] AS clerk_type,
    SUM(pages_kb) / 1024 AS pages_mb
FROM sys.dm_os_memory_clerks
GROUP BY [type]
ORDER BY SUM(pages_kb) DESC;
```

**(c) Memory grant waits / RESOURCE_SEMAPHORE** — surfaced from `waits.sql` (a rising `RESOURCE_SEMAPHORE` wait = query memory pressure).

| Metric | Threshold key |
|---|---|
| `page_life_expectancy` (vs dynamic floor) | `memory.ple_warn/crit` |
| `memory_grants_pending` | `memory.memory_grants_pending_warn` (>0 sustained = pressure) |
| `buffer_cache_hit_ratio` | context (noisy; PLE preferred) |
| `total_vs_target_server_memory` | flag if Total << Target (ramp-up or external pressure) |
| `RESOURCE_SEMAPHORE` wait share | context |

---

### 7.3 Disk usage & throughput — `collectors/io_disk.py` + `collectors/space.py`

**Objective:** file-level IO latency/throughput and free-space/growth for databases and drives.

**(a) IO throughput & latency per file** (cumulative — derive per-interval rates from two samples)

```sql
-- io_file_stats.sql : virtual file stats; latency derived as stall/IO
SELECT
    DB_NAME(vfs.database_id)               AS database_name,
    mf.type_desc                           AS file_type,      -- ROWS / LOG
    mf.physical_name,
    vfs.num_of_reads,
    vfs.num_of_writes,
    vfs.num_of_bytes_read,
    vfs.num_of_bytes_written,
    vfs.io_stall_read_ms,
    vfs.io_stall_write_ms,
    vfs.io_stall,
    -- point-in-time averages since restart (interval rates computed in derive.py):
    vfs.io_stall_read_ms  / NULLIF(vfs.num_of_reads,0)   AS avg_read_latency_ms,
    vfs.io_stall_write_ms / NULLIF(vfs.num_of_writes,0)  AS avg_write_latency_ms
FROM sys.dm_io_virtual_file_stats(NULL, NULL) AS vfs
JOIN sys.master_files AS mf
  ON mf.database_id = vfs.database_id AND mf.file_id = vfs.file_id;
```
`derive.py` computes **interval** latency = Δio_stall / Δio_count and **throughput** MB/s = Δbytes / Δseconds between consecutive fast samples, so a spike shows up as a 15-minute-window rate, not a since-restart average.

**(b) Database file space & autogrowth headroom**

```sql
-- space_db.sql : used/free space per data & log file (run per DB via sp_MSforeachdb or a loop;
-- preferred: iterate sys.databases in Python and run this in each DB context)
SELECT
    DB_NAME()                              AS database_name,
    f.name                                 AS logical_name,
    f.type_desc,
    CAST(f.size AS BIGINT) * 8 / 1024      AS size_mb,
    CAST(FILEPROPERTY(f.name,'SpaceUsed') AS BIGINT) * 8 / 1024 AS used_mb,
    (CAST(f.size AS BIGINT) - CAST(FILEPROPERTY(f.name,'SpaceUsed') AS BIGINT)) * 8 / 1024 AS free_mb,
    f.max_size, f.growth, f.is_percent_growth
FROM sys.database_files f;
```

**(c) Drive / volume free space** (server-scoped, one query)

```sql
-- space_drive.sql : free space on every volume that hosts a DB file (2008 R2 SP1+ / 2012+)
SELECT DISTINCT
    vs.volume_mount_point,
    vs.total_bytes / 1024 / 1024 / 1024                     AS total_gb,
    vs.available_bytes / 1024 / 1024 / 1024                 AS free_gb,
    CAST(vs.available_bytes * 100.0 / vs.total_bytes AS DECIMAL(5,2)) AS free_pct
FROM sys.master_files mf
CROSS APPLY sys.dm_os_volume_stats(mf.database_id, mf.file_id) vs;
```

**Legacy variant** (`space_drive_legacy.sql`, chosen when `sys.dm_os_volume_stats` is absent — pre-2008 R2 SP1). `xp_fixeddrives` returns **free MB only**, no volume total, so `free_pct` and total capacity are unavailable; the collector stores `free_gb`, leaves `total_gb`/`free_pct` NULL, and the analyzer switches this server to an **absolute free-MB threshold** instead of `drive_free_pct`:

```sql
-- space_drive_legacy.sql : free MB per drive letter (no total, no %)
CREATE TABLE #fixeddrives (drive CHAR(1), free_mb INT);
INSERT INTO #fixeddrives EXEC master..xp_fixeddrives;   -- undocumented but universally available
SELECT drive AS volume_mount_point,
       CAST(NULL AS DECIMAL(12,2)) AS total_gb,
       free_mb / 1024.0            AS free_gb,
       CAST(NULL AS DECIMAL(5,2))  AS free_pct
FROM #fixeddrives;
DROP TABLE #fixeddrives;
```
> `xp_fixeddrives` reports only drive letters (not mount points) and requires the monitoring login to have EXECUTE on it — noted in the provisioning script for legacy instances.

**(d) tempdb pressure** (daily/context) — tempdb file space + allocation contention via `sys.dm_db_task_space_usage` / `sys.dm_db_file_space_usage`; flag version-store growth and PFS/GAM/SGAM `PAGELATCH` waits.

| Metric | Threshold key |
|---|---|
| `avg_read_latency_ms` / `avg_write_latency_ms` (interval) | `io.read_latency_ms_*` / `io.write_latency_ms_*` |
| throughput MB/s read/write (interval) | context / capacity trend |
| `db_free_pct`, projected days-to-full (from 7-day growth slope) | `space.db_free_pct_*` |
| `drive_free_pct` | `space.drive_free_pct_*` |
| tempdb used MB, version store MB | context |

> Days-to-full is derived by fitting a simple linear slope over the 7-day `used_mb` series per file/drive; report shows "≈ N days at current growth."

---

### 7.4 Index optimization — `collectors/indexes.py` (daily tier)

**Objective:** fragmentation (rebuild/reorg candidates), missing indexes, and unused/duplicate indexes.

**(a) Fragmentation** — `LIMITED` scan mode (cheap; avoid `DETAILED` on prod). Run per user DB.

```sql
-- index_frag.sql : fragmentation for non-trivial indexes (run in each DB)
SELECT
    DB_NAME()                              AS database_name,
    OBJECT_SCHEMA_NAME(ips.object_id)      AS schema_name,
    OBJECT_NAME(ips.object_id)             AS table_name,
    i.name                                 AS index_name,
    ips.index_type_desc,
    ips.avg_fragmentation_in_percent,
    ips.page_count,
    CASE
        WHEN ips.avg_fragmentation_in_percent > 30 THEN 'REBUILD'
        WHEN ips.avg_fragmentation_in_percent >= 15 THEN 'REORGANIZE'
        ELSE 'OK'
    END AS recommendation
FROM sys.dm_db_index_physical_stats(DB_ID(), NULL, NULL, NULL, 'LIMITED') ips
JOIN sys.indexes i ON i.object_id = ips.object_id AND i.index_id = ips.index_id
WHERE ips.page_count >= 1000            -- ignore tiny indexes (config: index.min_page_count)
  AND ips.avg_fragmentation_in_percent >= 15
  AND i.index_id > 0                    -- skip heaps
ORDER BY ips.avg_fragmentation_in_percent DESC;
```

**(b) Missing-index recommendations** (server-scoped DMV group)

```sql
-- index_missing.sql : missing indexes ranked by estimated impact
SELECT TOP (50)
    DB_NAME(mid.database_id)               AS database_name,
    OBJECT_NAME(mid.object_id, mid.database_id) AS table_name,
    migs.avg_user_impact,
    migs.user_seeks + migs.user_scans      AS demand,
    migs.avg_total_user_cost,
    (migs.user_seeks + migs.user_scans) * migs.avg_total_user_cost * (migs.avg_user_impact/100.0)
                                           AS improvement_measure,
    mid.equality_columns, mid.inequality_columns, mid.included_columns
FROM sys.dm_db_missing_index_group_stats migs
JOIN sys.dm_db_missing_index_groups mig ON migs.group_handle = mig.index_group_handle
JOIN sys.dm_db_missing_index_details mid ON mig.index_handle = mid.index_handle
ORDER BY improvement_measure DESC;
```
> Report as **suggestions with impact score**, never auto-created — the DMV over-recommends and ignores write cost/overlap. `recommendations.py` also flags near-duplicate suggestions.

**(c) Unused / rarely-used indexes** (writes ≫ reads — candidates to drop)

```sql
-- index_usage.sql : non-clustered indexes with high maintenance cost and little read benefit (per DB)
SELECT
    DB_NAME()                              AS database_name,
    OBJECT_SCHEMA_NAME(i.object_id)        AS schema_name,
    OBJECT_NAME(i.object_id)               AS table_name,
    i.name                                 AS index_name,
    us.user_seeks, us.user_scans, us.user_lookups,
    us.user_updates,
    (us.user_seeks + us.user_scans + us.user_lookups) AS reads,
    us.last_user_seek, us.last_user_scan
FROM sys.indexes i
LEFT JOIN sys.dm_db_index_usage_stats us
       ON us.object_id = i.object_id AND us.index_id = i.index_id AND us.database_id = DB_ID()
WHERE i.type_desc = 'NONCLUSTERED' AND i.is_primary_key = 0 AND i.is_unique_constraint = 0
  AND OBJECTPROPERTY(i.object_id,'IsUserTable') = 1
  AND ISNULL(us.user_updates,0) > 100
  AND (us.user_seeks + us.user_scans + us.user_lookups) = 0   -- zero reads since last stats reset
ORDER BY us.user_updates DESC;
```
> Caveat: `dm_db_index_usage_stats` resets on restart (and, in some patch levels, on index rebuild). Report includes **instance uptime** so "0 reads" is interpreted against how long counters have accumulated. Duplicate-index detection compares key+included column sets within a table.

| Output | Nature |
|---|---|
| fragmentation candidates (>15% reorg, >30% rebuild, ≥1000 pages) | recommendation list + generated (non-executed) TSQL |
| missing indexes ranked by `improvement_measure` | recommendation list |
| unused/duplicate indexes | recommendation list (drop candidates) |

---

### 7.5 Statistics — `collectors/statistics.py` (daily tier)

**Objective:** find stale statistics that can mislead the optimizer.

```sql
-- stats_age.sql : statistics age + modification counter (run per DB; sys.dm_db_stats_properties = 2012 SP1+)
SELECT
    DB_NAME()                              AS database_name,
    OBJECT_SCHEMA_NAME(s.object_id)        AS schema_name,
    OBJECT_NAME(s.object_id)               AS table_name,
    s.name                                 AS stats_name,
    sp.last_updated,
    sp.rows,
    sp.rows_sampled,
    sp.modification_counter,
    CAST(sp.modification_counter * 1.0 / NULLIF(sp.rows,0) AS DECIMAL(6,3)) AS modification_ratio,
    DATEDIFF(DAY, sp.last_updated, GETDATE()) AS days_since_update,
    s.auto_created, s.user_created, s.no_recompute
FROM sys.stats s
CROSS APPLY sys.dm_db_stats_properties(s.object_id, s.stats_id) sp
JOIN sys.objects o ON o.object_id = s.object_id
WHERE o.is_ms_shipped = 0
  AND sp.rows >= 1000
  AND ( DATEDIFF(DAY, sp.last_updated, GETDATE()) >= 7        -- stats.stale_days_warn
        OR sp.modification_counter * 1.0 / NULLIF(sp.rows,0) >= 0.20 )  -- stats.modification_ratio_warn
ORDER BY modification_ratio DESC, days_since_update DESC;
```

**Legacy variant** (`stats_age_legacy.sql`, chosen when `sys.dm_db_stats_properties` is absent — pre-2008 R2 SP2 / pre-2012 SP1). Uses `STATS_DATE()` for the last-updated timestamp (exact) and the deprecated `sys.sysindexes.rowmodctr` for modifications (per-table, not per-statistic, and reset on stats update — so the mod-ratio is **approximate**):

```sql
-- stats_age_legacy.sql : last-updated is exact; rowmodctr is an approximation of modifications
SELECT
    DB_NAME()                              AS database_name,
    OBJECT_SCHEMA_NAME(s.object_id)        AS schema_name,
    OBJECT_NAME(s.object_id)               AS table_name,
    s.name                                 AS stats_name,
    STATS_DATE(s.object_id, s.stats_id)    AS last_updated,
    si.rowcnt                              AS [rows],
    si.rowmodctr                           AS modification_counter,   -- deprecated, per-table approx
    CAST(si.rowmodctr * 1.0 / NULLIF(si.rowcnt,0) AS DECIMAL(6,3)) AS modification_ratio,
    DATEDIFF(DAY, STATS_DATE(s.object_id, s.stats_id), GETDATE()) AS days_since_update,
    s.no_recompute
FROM sys.stats s
JOIN sys.sysindexes si ON si.id = s.object_id AND si.indid IN (0,1)   -- heap/clustered rowmodctr
JOIN sys.objects o ON o.object_id = s.object_id
WHERE o.is_ms_shipped = 0 AND si.rowcnt >= 1000
  AND ( DATEDIFF(DAY, STATS_DATE(s.object_id, s.stats_id), GETDATE()) >= 7
        OR si.rowmodctr * 1.0 / NULLIF(si.rowcnt,0) >= 0.20 )
ORDER BY modification_ratio DESC, days_since_update DESC;
```
> The report flags legacy-path stats findings as "modification estimate (rowmodctr)" so the approximate mod-ratio isn't read as exact. `last_updated` is trustworthy on both paths.

Also capture DB-level flags from `sys.databases`: `is_auto_update_stats_on`, `is_auto_update_stats_async_on`, `is_auto_create_stats_on` — a stale-stats finding on a DB with auto-update **off** is escalated (root cause), and a very large table hitting the old fixed threshold is noted (pre-2016 / no trace flag 2371 behavior).

| Metric | Threshold key |
|---|---|
| `days_since_update` | `stats.stale_days_warn` |
| `modification_ratio` | `stats.modification_ratio_warn` |
| DB auto-update-stats off + stale findings | escalation flag |

Output: list of stale statistics with generated (non-executed) `UPDATE STATISTICS ... WITH ...` guidance.

---

### 7.6 Query history — `collectors/query_store.py` (+ `plan_cache.py` fallback)

**Objective:** top queries by duration, CPU, reads, and execution count, with day-over-day / intraday comparison and **occurrence counts**.

**Primary path — Query Store (SQL 2016+ where Query Store is ENABLED per database).** Query Store persists history across restarts, so it gives true "how often did this run and how long" over time — exactly the "durations and occurrences" objective.

```sql
-- querystore_top.sql : top queries over a window (run per DB where QS is on)
DECLARE @from DATETIME2 = DATEADD(DAY, -1, SYSUTCDATETIME());   -- last 24h; configurable
SELECT TOP (25)
    q.query_id,
    qt.query_sql_text,
    SUM(rs.count_executions)                                   AS executions,
    SUM(rs.count_executions * rs.avg_duration) / 1000.0        AS total_duration_ms,
    AVG(rs.avg_duration) / 1000.0                              AS avg_duration_ms,
    MAX(rs.max_duration) / 1000.0                              AS max_duration_ms,
    SUM(rs.count_executions * rs.avg_cpu_time) / 1000.0        AS total_cpu_ms,
    SUM(rs.count_executions * rs.avg_logical_io_reads)         AS total_logical_reads,
    MIN(rsi.start_time)                                        AS window_start,
    MAX(rsi.end_time)                                          AS window_end
FROM sys.query_store_runtime_stats rs
JOIN sys.query_store_runtime_stats_interval rsi ON rs.runtime_stats_interval_id = rsi.runtime_stats_interval_id
JOIN sys.query_store_plan p  ON p.plan_id = rs.plan_id
JOIN sys.query_store_query q ON q.query_id = p.query_id
JOIN sys.query_store_query_text qt ON qt.query_text_id = q.query_text_id
WHERE rsi.start_time >= @from
GROUP BY q.query_id, qt.query_sql_text
ORDER BY total_duration_ms DESC;
```
The same shape is re-run ordered by `total_cpu_ms`, `total_logical_reads`, and `executions` to produce the four "top" lists. Because Query Store is durable, **day-over-day comparison** = compare aggregates for `@from = -1 day` window vs the prior day for the same `query_id` (stable identity), highlighting queries that regressed (avg duration up X%) or newly appeared.

**Feature gate (`applies_to`):**
- Instance `>= 2016` **and** database `is_query_store_on = 1` → use Query Store path.
- Otherwise (any pre-2016 instance, or a 2016+ DB with QS off) → plan-cache path below. On a mixed fleet this is a **normal, expected** path for a chunk of the servers, not a rare fallback. The report labels each server's query section with its source ("Query Store — durable history" vs "Plan cache — since last restart, volatile") so the two are never compared as equals.

**Fallback path — plan cache (2012/2014, or DBs with QS off):**

```sql
-- plan_cache_top.sql : top statements from dm_exec_query_stats (volatile: cleared on restart/memory pressure)
SELECT TOP (25)
    qs.query_hash,
    qs.execution_count                                        AS executions,
    qs.total_elapsed_time / 1000.0                            AS total_duration_ms,
    qs.total_elapsed_time / NULLIF(qs.execution_count,0) / 1000.0 AS avg_duration_ms,
    qs.max_elapsed_time / 1000.0                              AS max_duration_ms,
    qs.total_worker_time / 1000.0                             AS total_cpu_ms,
    qs.total_logical_reads,
    qs.creation_time, qs.last_execution_time,
    SUBSTRING(st.text, (qs.statement_start_offset/2)+1,
        ((CASE qs.statement_end_offset WHEN -1 THEN DATALENGTH(st.text)
              ELSE qs.statement_end_offset END - qs.statement_start_offset)/2)+1) AS statement_text
FROM sys.dm_exec_query_stats qs
CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
ORDER BY qs.total_elapsed_time DESC;
```
For the fallback, day-over-day is approximated by persisting each daily snapshot in the repository (`mon.query_top`) and diffing by `query_hash` (identity is weaker than Query Store's `query_id`, but usable).

**Blocking / waits context (fast tier)** — `collectors/blocking.py` and `collectors/waits.py` back the query story:

```sql
-- blocking.sql : current blocking chains
SELECT
    r.session_id                           AS blocked_spid,
    r.blocking_session_id                  AS blocking_spid,
    r.wait_type, r.wait_time / 1000.0      AS wait_seconds,
    r.wait_resource,
    DB_NAME(r.database_id)                 AS database_name,
    SUBSTRING(t.text,(r.statement_start_offset/2)+1,
        ((CASE r.statement_end_offset WHEN -1 THEN DATALENGTH(t.text)
              ELSE r.statement_end_offset END - r.statement_start_offset)/2)+1) AS blocked_stmt
FROM sys.dm_exec_requests r
CROSS APPLY sys.dm_exec_sql_text(r.sql_handle) t
WHERE r.blocking_session_id <> 0;
```

```sql
-- waits.sql : top waits (used for CPU signal%, memory RESOURCE_SEMAPHORE, IO PAGEIOLATCH, etc.)
SELECT TOP (15)
    wait_type,
    wait_time_ms,
    waiting_tasks_count,
    wait_time_ms - signal_wait_time_ms     AS resource_wait_ms,
    signal_wait_time_ms
FROM sys.dm_os_wait_stats
WHERE wait_type NOT IN ( /* same benign list as §7.1(b) */ )
  AND wait_time_ms > 0
ORDER BY wait_time_ms DESC;
```

| Output | Nature |
|---|---|
| top-N by duration / CPU / reads / executions | per-server list, source-labeled |
| day-over-day regressions | delta list (avg duration +%, exec count change) |
| current blocking chains | fast-tier alertable |
| top waits | context + drives CPU/memory/IO interpretation |

---

### 7.7 Instance metadata — `collectors/instance_meta.py` (daily + first fast run)

Captures per instance: `@@VERSION`, `SERVERPROPERTY('ProductMajorVersion')` / `ProductLevel` (SP) / `ProductUpdateLevel` (CU) / `Edition`, `sqlserver_start_time` (uptime — needed to interpret cumulative counters), core count / scheduler count, max/min server memory, and per-DB flags (recovery model, Query Store state, auto-stats flags, compatibility level).

`version.py` turns this into a per-instance **feature set** that drives every collector's SQL-variant choice (§2.1). Because service-pack level — not just major version — decides whether `dm_db_stats_properties` and `dm_os_volume_stats` exist, the resolver **probes actual object availability** rather than trusting a version→feature table:

```sql
-- feature_probe.sql : cheap, run once per instance, cached on mon.server, refreshed daily
-- NOTE: parse ProductVersion for the major/minor version. Do NOT use SERVERPROPERTY('ProductMajorVersion') —
--       that property was only added in SQL Server 2014 SP2 and returns NULL on the older boxes we care about.
SELECT
    SERVERPROPERTY('ProductVersion')                                   AS product_version,   -- all versions, e.g. 10.50.6000.34
    CONVERT(INT, PARSENAME(CONVERT(varchar(32), SERVERPROPERTY('ProductVersion')), 4)) AS major_version, -- 10,11,12,13...
    CONVERT(INT, PARSENAME(CONVERT(varchar(32), SERVERPROPERTY('ProductVersion')), 3)) AS minor_version, -- 0=2008 vs 50=2008 R2
    SERVERPROPERTY('ProductLevel')                                     AS product_level,   -- RTM/SP1/SP2/...
    SERVERPROPERTY('Edition')                                          AS edition,
    CONVERT(INT, SERVERPROPERTY('EngineEdition'))                      AS engine_edition,  -- 5=Azure SQL DB, 8=Managed Instance, else box
    -- authoritative feature signals (object existence trumps any version→feature guess):
    CASE WHEN OBJECT_ID('sys.dm_db_stats_properties') IS NOT NULL THEN 1 ELSE 0 END AS has_stats_properties,
    CASE WHEN OBJECT_ID('sys.dm_os_volume_stats')    IS NOT NULL THEN 1 ELSE 0 END AS has_volume_stats,
    CASE WHEN OBJECT_ID('sys.query_store_query')     IS NOT NULL THEN 1 ELSE 0 END AS has_query_store_objects,
    CASE WHEN OBJECT_ID('sys.dm_xe_sessions')        IS NOT NULL THEN 1 ELSE 0 END AS has_extended_events;  -- 2008+; drives deadlock capture
```
> **Version parsing:** `ProductVersion` always has four dot-parts (`a.b.c.d`), so `PARSENAME(...,4)` = major and `PARSENAME(...,3)` = minor; 2008 and 2008 R2 share major 10 and differ only by minor (`0` vs `50`), which matters because `dm_os_volume_stats` exists on 2008 R2 SP1 but never on plain 2008. The version numbers are context/reporting; **the `OBJECT_ID(...)` probes are what actually gate the collectors**, since service-pack level (not major version) decides whether `dm_db_stats_properties` (SP-gated) and `dm_os_volume_stats` (SP-gated) are present — object existence is correct even on unusual patch levels.

The resolved feature flags (`has_volume_stats`, `has_stats_properties`, `has_query_store`, per-DB `is_query_store_on`, `has_query_hash`, `has_buffer_node_ple`, `has_extended_events`) plus `major_version`/`minor_version`/`product_level`/`engine_edition` are stored on `mon.server` and captioned on the report ("counters since restart"; "limited on this version" badges). Because it's cached and refreshed on the daily pass, a freshly-patched instance is re-detected within a day.

---

### 7.8 Deadlocks & blocking depth — `collectors/deadlocks.py`, `collectors/blocking.py`

**Why this is separate from the blocking snapshot.** Blocking is a *state* — one session waiting on a lock another holds — so polling `sys.dm_exec_requests` catches it while it lasts (§7.6). A **deadlock** is an *event*: SQL Server's lock monitor detects the cycle and kills a victim in milliseconds, so it is essentially never visible to a 15-minute poll. Deadlocks must be collected **after the fact** from a persistent event source, not sampled.

**Source — the always-on `system_health` Extended Events session (2008+).** Every box already runs `system_health`, and it captures the `xml_deadlock_report` by default — nothing to enable on the monitored servers. The daily tier reads new deadlock graphs since the last run. Two targets:

- **Preferred — event_file target** (persists across ring-buffer rollover, so nothing is missed between daily runs):

```sql
-- deadlocks.sql : new deadlock graphs from the system_health event_file (2012+ layout)
SELECT
    x.event_data.value('(event/@timestamp)[1]','datetime2')                          AS deadlock_time_utc,
    x.event_data.query('event/data[@name="xml_report"]/value/deadlock')              AS deadlock_graph
FROM (
    SELECT CAST(event_data AS XML) AS event_data
    FROM sys.fn_xe_file_target_read_file('system_health*.xel', NULL, NULL, NULL)
    WHERE object_name = 'xml_deadlock_report'
) AS x
WHERE x.event_data.value('(event/@timestamp)[1]','datetime2') > @since_utc;  -- per-server high-water mark
```

- **Fallback — ring_buffer target** (in memory, capped/recent — used if the file target isn't readable):

```sql
WITH xe AS (
    SELECT CAST(xet.target_data AS XML) AS target_xml
    FROM sys.dm_xe_session_targets xet
    JOIN sys.dm_xe_sessions xes ON xes.address = xet.event_session_address
    WHERE xes.name = 'system_health' AND xet.target_name = 'ring_buffer'
)
SELECT evt.value('(@timestamp)[1]','datetime2')  AS deadlock_time_utc,
       evt.query('.')                            AS deadlock_graph
FROM xe CROSS APPLY target_xml.nodes('//RingBufferTarget/event[@name="xml_deadlock_report"]') AS q(evt);
```

**Parsing.** From each graph the collector extracts: `deadlock_time_utc`, the **victim** SPID, participant count, the databases/objects and lock resources involved, and the victim's statement/procedure. The **full graph XML is stored** so a DBA can open the exact cycle. Parsing happens in Python (or in T-SQL via `.value()`/`.nodes()`); either way the raw graph is retained.

**Dedup / incremental.** Each server keeps a **high-water mark** (last ingested `deadlock_time_utc`, on a small `mon.collector_watermark` table) so re-reading the file only ingests new events. A `dedup_key = server_id + deadlock_time + victim + resource-hash` and a unique index make ingestion idempotent if a run overlaps.

**Version / edition gating (`applies_to`, via `has_extended_events`):**
- **2012+ box:** event_file target (preferred).
- **2008 / 2008 R2:** `system_health` exists; the `xml_report` node layout differs slightly and the file target may not be readable — use the ring_buffer variant and the 2008 XML shape.
- **2005 (no Extended Events):** fall back to enabling **trace flag 1222** and scraping the ERRORLOG via `xp_readerrorlog` for deadlock entries — heavier and lossy; flagged as an edge case, built only if 2005 boxes are confirmed present.
- **Azure SQL DB** (`EngineEdition = 5`): no `system_health` in the same form — use `sys.event_log` / the database-scoped XE; Managed Instance (`8`) behaves like box. Gated, not assumed.

**Blocking-depth enhancement (optional, `blocking.py`).** The 15-min blocking poll catches only sustained blocking present at the sample instant. For teams that need every blocking episode (not just what's live at :00/:15/:30/:45), the **Blocked Process Report** can be captured the same way as deadlocks: set `sp_configure 'blocked process threshold'` (seconds) on the instance and read the `blocked_process_report` events from a lightweight XE session. This is **opt-in per server** (it requires a config change on the monitored box), documented but off by default; the default poll stays the zero-touch baseline.

**Tier:** deadlocks run on the **daily** tier (system_health persists them, so one daily sweep captures the last 24h) with an **optional fast-tier pass** for near-real-time deadlock alerting where a tag requests it.

---

## 8. Storage — central SQL Server repository (`DBA_Monitoring`) + Parquet exports

### 8.1 Design

- The **operational store is a dedicated SQL Server database, `DBA_Monitoring`**, with all objects under a `mon` schema. The analyzer, report generator, and day-over-day comparisons read from it with plain T-SQL. Holds **7 days** of raw samples.
- **Host it on a separate / non-critical instance — not one of the 40 monitored prod boxes.** Reasons: (1) collection + retention load never touches production; (2) if a monitored server goes down, monitoring keeps running; (3) you can grant the collector write rights on one repository instead of storing state on every prod box. A modest instance (or an existing DBA/utility instance) is fine — sizing in §8.4.
- **Parquet/CSV** under `data/exports/YYYY-MM-DD/` remain **cold export artifacts** written each run (the CSV/Excel deliverables, and a portable archive). The repository is the source of truth; exports are optional and independently retained.
- **One table per collector** under `mon`, plus `mon.runs` (run bookkeeping), `mon.server` (a small dimension: one row per monitored instance, so samples key on a compact `server_id` instead of repeating the name), and `mon.findings` (evaluated threshold breaches). Every metric row carries `server_id`, `run_id`, `collected_at_utc`.
- **Concurrent writers are fine and expected** — unlike SQLite, SQL Server has no single-writer limit, so the parallel per-server workers can each bulk-insert their own rows concurrently. Writes use pyodbc **`fast_executemany=True`** batched inserts (≈5000 rows/batch) — or table-valued parameters for the hottest tables — one transaction per collector per server, append-only.
- **Keys & clustering:** each sample table has an `IDENTITY` `BIGINT` surrogate PK that is **nonclustered**, and a **clustered index on `(collected_at_utc, server_id)`** so 7-day-window and retention-by-date scans are range scans and pruning deletes/switches contiguous data. `run_id` is a `UNIQUEIDENTIFIER`.
- **The repository monitors itself:** `DBA_Monitoring` is added to the fleet (or at least its own space/space-drive tracked) so the store can't silently fill a disk. Its own indexes are kept healthy on a weekly job (`retention.rebuild_repo_indexes`).

### 8.2 Core tables (illustrative T-SQL DDL — `mon` schema in `DBA_Monitoring`)

```sql
CREATE DATABASE DBA_Monitoring;   -- host on a dedicated/non-critical instance
GO
USE DBA_Monitoring;
GO
CREATE SCHEMA mon;
GO

-- Dimension: one row per monitored instance (samples key on server_id, not the name)
CREATE TABLE mon.server (
    server_id       INT IDENTITY(1,1) PRIMARY KEY,
    server_name     NVARCHAR(128) NOT NULL UNIQUE,   -- matches servers.yml name
    host_name       NVARCHAR(255) NULL,
    tags            NVARCHAR(400) NULL,              -- csv of tags
    product_version NVARCHAR(64)  NULL,              -- e.g. 10.50.6000.34
    major_version   INT NULL,                        -- 10,11,12,13...
    minor_version   INT NULL,                        -- 0 vs 50 (2008 vs 2008 R2)
    product_level   NVARCHAR(16)  NULL,              -- RTM/SP1/SP2...
    edition         NVARCHAR(64)  NULL,
    engine_edition  INT NULL,                        -- 5=Azure SQL DB, 8=MI, else box
    feature_flags   NVARCHAR(400) NULL,              -- json: has_volume_stats/has_stats_properties/has_query_store/...
    features_checked_utc DATETIME2(0) NULL,          -- when the probe last ran (refreshed daily)
    is_enabled      BIT NOT NULL DEFAULT 1
);

CREATE TABLE mon.runs (
    run_id         UNIQUEIDENTIFIER NOT NULL PRIMARY KEY,
    tier           VARCHAR(8) NOT NULL,             -- fast | daily
    started_utc    DATETIME2(0) NOT NULL,
    finished_utc   DATETIME2(0) NULL,
    servers_ok     INT NULL,
    servers_failed INT NULL,
    notes          NVARCHAR(1000) NULL
);

CREATE TABLE mon.server_status (          -- per server per run: reachability + timings
    run_id UNIQUEIDENTIFIER NOT NULL, server_id INT NOT NULL,
    ok BIT NOT NULL, error NVARCHAR(1000) NULL,
    duration_ms INT NULL, collected_at_utc DATETIME2(0) NOT NULL
);

CREATE TABLE mon.cpu_sample (
    id BIGINT IDENTITY(1,1) NOT NULL,
    run_id UNIQUEIDENTIFIER NOT NULL, server_id INT NOT NULL, collected_at_utc DATETIME2(0) NOT NULL,
    sql_cpu_pct TINYINT, other_process_pct TINYINT, system_idle_pct TINYINT,
    signal_wait_pct DECIMAL(5,2), runnable_tasks INT,
    CONSTRAINT pk_cpu_sample PRIMARY KEY NONCLUSTERED (id)
);
CREATE CLUSTERED INDEX cix_cpu_sample ON mon.cpu_sample (collected_at_utc, server_id);

CREATE TABLE mon.memory_sample (
    id BIGINT IDENTITY(1,1) NOT NULL,
    run_id UNIQUEIDENTIFIER NOT NULL, server_id INT NOT NULL, collected_at_utc DATETIME2(0) NOT NULL,
    page_life_expectancy INT, ple_dynamic_floor INT,
    memory_grants_pending INT, buffer_cache_hit_ratio DECIMAL(5,2),
    total_server_memory_mb INT, target_server_memory_mb INT,
    CONSTRAINT pk_memory_sample PRIMARY KEY NONCLUSTERED (id)
);
CREATE CLUSTERED INDEX cix_memory_sample ON mon.memory_sample (collected_at_utc, server_id);

CREATE TABLE mon.io_file_sample (
    id BIGINT IDENTITY(1,1) NOT NULL,
    run_id UNIQUEIDENTIFIER NOT NULL, server_id INT NOT NULL, collected_at_utc DATETIME2(0) NOT NULL,
    database_name NVARCHAR(128), file_type VARCHAR(8), physical_name NVARCHAR(260),
    num_of_reads BIGINT, num_of_writes BIGINT, bytes_read BIGINT, bytes_written BIGINT,
    io_stall_read_ms BIGINT, io_stall_write_ms BIGINT,
    avg_read_latency_ms DECIMAL(10,2), avg_write_latency_ms DECIMAL(10,2),        -- since-restart
    interval_read_latency_ms DECIMAL(10,2), interval_write_latency_ms DECIMAL(10,2),  -- derived
    interval_read_mb_s DECIMAL(12,2), interval_write_mb_s DECIMAL(12,2),
    CONSTRAINT pk_io_file_sample PRIMARY KEY NONCLUSTERED (id)
);
CREATE CLUSTERED INDEX cix_io_file_sample ON mon.io_file_sample (collected_at_utc, server_id);

CREATE TABLE mon.space_db_sample (
    id BIGINT IDENTITY(1,1) NOT NULL,
    run_id UNIQUEIDENTIFIER NOT NULL, server_id INT NOT NULL, collected_at_utc DATETIME2(0) NOT NULL,
    database_name NVARCHAR(128), logical_name NVARCHAR(128), file_type VARCHAR(8),
    size_mb BIGINT, used_mb BIGINT, free_mb BIGINT, free_pct DECIMAL(5,2),
    max_size_mb BIGINT, is_percent_growth BIT,
    CONSTRAINT pk_space_db_sample PRIMARY KEY NONCLUSTERED (id)
);
CREATE CLUSTERED INDEX cix_space_db_sample ON mon.space_db_sample (collected_at_utc, server_id);

CREATE TABLE mon.space_drive_sample (
    id BIGINT IDENTITY(1,1) NOT NULL,
    run_id UNIQUEIDENTIFIER NOT NULL, server_id INT NOT NULL, collected_at_utc DATETIME2(0) NOT NULL,
    volume_mount_point NVARCHAR(260), total_gb DECIMAL(12,2), free_gb DECIMAL(12,2), free_pct DECIMAL(5,2),
    CONSTRAINT pk_space_drive_sample PRIMARY KEY NONCLUSTERED (id)
);
CREATE CLUSTERED INDEX cix_space_drive_sample ON mon.space_drive_sample (collected_at_utc, server_id);

CREATE TABLE mon.index_frag (           -- daily
    id BIGINT IDENTITY(1,1) NOT NULL,
    run_id UNIQUEIDENTIFIER NOT NULL, server_id INT NOT NULL, collected_at_utc DATETIME2(0) NOT NULL,
    database_name NVARCHAR(128), schema_name NVARCHAR(128), table_name NVARCHAR(128), index_name NVARCHAR(128),
    index_type NVARCHAR(60), avg_fragmentation_pct DECIMAL(5,2), page_count BIGINT, recommendation VARCHAR(12),
    CONSTRAINT pk_index_frag PRIMARY KEY NONCLUSTERED (id)
);
CREATE CLUSTERED INDEX cix_index_frag ON mon.index_frag (collected_at_utc, server_id);

CREATE TABLE mon.index_missing (        -- daily
    id BIGINT IDENTITY(1,1) NOT NULL,
    run_id UNIQUEIDENTIFIER NOT NULL, server_id INT NOT NULL, collected_at_utc DATETIME2(0) NOT NULL,
    database_name NVARCHAR(128), table_name NVARCHAR(128), avg_user_impact DECIMAL(6,2), demand BIGINT,
    improvement_measure FLOAT, equality_columns NVARCHAR(MAX), inequality_columns NVARCHAR(MAX), included_columns NVARCHAR(MAX),
    CONSTRAINT pk_index_missing PRIMARY KEY NONCLUSTERED (id)
);
CREATE CLUSTERED INDEX cix_index_missing ON mon.index_missing (collected_at_utc, server_id);

CREATE TABLE mon.index_unused (         -- daily
    id BIGINT IDENTITY(1,1) NOT NULL,
    run_id UNIQUEIDENTIFIER NOT NULL, server_id INT NOT NULL, collected_at_utc DATETIME2(0) NOT NULL,
    database_name NVARCHAR(128), schema_name NVARCHAR(128), table_name NVARCHAR(128), index_name NVARCHAR(128),
    reads BIGINT, user_updates BIGINT, last_user_seek DATETIME2(0) NULL,
    CONSTRAINT pk_index_unused PRIMARY KEY NONCLUSTERED (id)
);
CREATE CLUSTERED INDEX cix_index_unused ON mon.index_unused (collected_at_utc, server_id);

CREATE TABLE mon.stats_stale (          -- daily
    id BIGINT IDENTITY(1,1) NOT NULL,
    run_id UNIQUEIDENTIFIER NOT NULL, server_id INT NOT NULL, collected_at_utc DATETIME2(0) NOT NULL,
    database_name NVARCHAR(128), schema_name NVARCHAR(128), table_name NVARCHAR(128), stats_name NVARCHAR(128),
    last_updated DATETIME2(0) NULL, [rows] BIGINT, modification_counter BIGINT,
    modification_ratio DECIMAL(6,3), days_since_update INT, no_recompute BIT,
    CONSTRAINT pk_stats_stale PRIMARY KEY NONCLUSTERED (id)
);
CREATE CLUSTERED INDEX cix_stats_stale ON mon.stats_stale (collected_at_utc, server_id);

CREATE TABLE mon.query_top (            -- daily; source = 'query_store' | 'plan_cache'
    id BIGINT IDENTITY(1,1) NOT NULL,
    run_id UNIQUEIDENTIFIER NOT NULL, server_id INT NOT NULL, collected_at_utc DATETIME2(0) NOT NULL,
    source VARCHAR(12), database_name NVARCHAR(128), query_identity NVARCHAR(64),  -- query_id or query_hash
    statement_text NVARCHAR(MAX), executions BIGINT,
    total_duration_ms DECIMAL(18,2), avg_duration_ms DECIMAL(18,2), max_duration_ms DECIMAL(18,2),
    total_cpu_ms DECIMAL(18,2), total_logical_reads BIGINT, rank_metric VARCHAR(10),  -- duration|cpu|reads|exec
    CONSTRAINT pk_query_top PRIMARY KEY NONCLUSTERED (id)
);
CREATE CLUSTERED INDEX cix_query_top ON mon.query_top (collected_at_utc, server_id, rank_metric);

CREATE TABLE mon.blocking_event (       -- fast
    id BIGINT IDENTITY(1,1) NOT NULL,
    run_id UNIQUEIDENTIFIER NOT NULL, server_id INT NOT NULL, collected_at_utc DATETIME2(0) NOT NULL,
    blocked_spid INT, blocking_spid INT, wait_type NVARCHAR(60),
    wait_seconds DECIMAL(10,2), database_name NVARCHAR(128), blocked_stmt NVARCHAR(MAX),
    CONSTRAINT pk_blocking_event PRIMARY KEY NONCLUSTERED (id)
);
CREATE CLUSTERED INDEX cix_blocking_event ON mon.blocking_event (collected_at_utc, server_id);

CREATE TABLE mon.deadlock_event (       -- daily (from system_health XE); event, not a poll snapshot
    id BIGINT IDENTITY(1,1) NOT NULL,
    run_id UNIQUEIDENTIFIER NOT NULL, server_id INT NOT NULL, collected_at_utc DATETIME2(0) NOT NULL,
    deadlock_time_utc DATETIME2(3) NOT NULL,       -- when the deadlock actually occurred
    victim_spid INT, participant_count INT,
    database_name NVARCHAR(128), objects NVARCHAR(MAX), victim_statement NVARCHAR(MAX),
    deadlock_graph XML NULL,                        -- full graph for drill-down
    dedup_key NVARCHAR(200) NOT NULL,
    CONSTRAINT pk_deadlock_event PRIMARY KEY NONCLUSTERED (id)
);
CREATE CLUSTERED INDEX cix_deadlock_event ON mon.deadlock_event (deadlock_time_utc, server_id);
CREATE UNIQUE NONCLUSTERED INDEX ux_deadlock_dedup ON mon.deadlock_event (server_id, dedup_key);

CREATE TABLE mon.collector_watermark (  -- incremental high-water marks (e.g. last ingested deadlock per server)
    server_id INT NOT NULL, collector VARCHAR(32) NOT NULL,
    last_value_utc DATETIME2(3) NULL, updated_utc DATETIME2(0) NOT NULL,
    CONSTRAINT pk_collector_watermark PRIMARY KEY (server_id, collector)
);

CREATE TABLE mon.wait_sample (          -- fast
    id BIGINT IDENTITY(1,1) NOT NULL,
    run_id UNIQUEIDENTIFIER NOT NULL, server_id INT NOT NULL, collected_at_utc DATETIME2(0) NOT NULL,
    wait_type NVARCHAR(60), wait_time_ms BIGINT, resource_wait_ms BIGINT,
    signal_wait_time_ms BIGINT, waiting_tasks_count BIGINT,
    CONSTRAINT pk_wait_sample PRIMARY KEY NONCLUSTERED (id)
);
CREATE CLUSTERED INDEX cix_wait_sample ON mon.wait_sample (collected_at_utc, server_id);

CREATE TABLE mon.findings (             -- output of the analyzer
    id BIGINT IDENTITY(1,1) NOT NULL,
    run_id UNIQUEIDENTIFIER NOT NULL, server_id INT NOT NULL, created_utc DATETIME2(0) NOT NULL,
    category VARCHAR(12),          -- cpu|memory|io|space|index|stats|query|blocking|deadlock
    severity VARCHAR(6),           -- info|warn|crit
    metric NVARCHAR(64), observed FLOAT, threshold FLOAT,
    message NVARCHAR(1000), details_json NVARCHAR(MAX), fingerprint NVARCHAR(200),  -- dedup key for alerting
    CONSTRAINT pk_findings PRIMARY KEY NONCLUSTERED (id)
);
CREATE CLUSTERED INDEX cix_findings ON mon.findings (created_utc, server_id);
CREATE NONCLUSTERED INDEX ix_findings_run_sev ON mon.findings (run_id, severity) INCLUDE (category, metric);
CREATE NONCLUSTERED INDEX ix_findings_fingerprint ON mon.findings (fingerprint, created_utc);  -- alert cooldown lookups
```

> **Notes:** reserved words (`rows`) are bracket-quoted. `NVARCHAR(MAX)` columns (statement text, missing-index column lists, details) are off-row and don't bloat the clustered index. Foreign keys to `mon.server`/`mon.runs` are optional — skipped on the hot sample tables to keep bulk inserts fast; referential integrity is enforced in the collector, which resolves/creates the `server_id` once per run and caches it.

### 8.3 Bulk-write path

Per collector per server: build the DataFrame → resolve `server_id` (cached) → `cursor.fast_executemany = True` → `executemany(INSERT ...)` in batches of ~5000 → commit. Because SQL Server allows concurrent writers, the `max_workers` pool writes in parallel; each worker owns its rows so there's no contention beyond normal page latches. For the very high-volume fast tables, an optional **table-valued parameter** (a `mon.<t>_TVP` type + a thin insert proc) is the faster path and is noted per table.

### 8.4 Sizing (rough, for a dedicated repo instance)

At 40 servers, fast tier every 15 min (96 runs/day) and daily tier once, the raw 7-day footprint is small — dominated by `io_file_sample` (per-file rows × 96/day) and `query_top`/`blocking`. Order of magnitude: **low single-digit GB** for 7 days at 40 servers; `NVARCHAR(MAX)` statement text is the main variable. Provision the repo DB at, say, 10–20 GB data + right-sized log (SIMPLE or BULK_LOGGED recovery is fine for a monitoring store) and let the 7-day prune hold it steady. Confirm against real cardinality after week one.

### 8.5 Parquet export

After each run, each collector's DataFrame is also written to
`data/exports/{date}/{tier}/{collector}_{run_id}.parquet` (+ `.csv` for the Excel/CSV deliverables). This is an optional cold archive independent of the repository; partitioning by date keeps files small for later pandas/DuckDB analysis. The daily Excel workbook (§10.3) is assembled from the repository (or these exports).

---

## 9. Retention & housekeeping — `storage/retention.py`

- **Raw retention = 7 days.** After each daily run, delete rows where `collected_at_utc < DATEADD(day,-7,SYSUTCDATETIME())` from every `mon.*_sample` / event table and from `mon.findings`. Because the clustered key leads with `collected_at_utc`, this is a range delete. `mon.runs` / `mon.server_status` kept longer (e.g. 30 days) for uptime/SLA reporting — tiny rows. `mon.server` / `mon.collector_watermark` are permanent state.
- **Deadlocks kept longer (config `retention.deadlock_days`, default 90):** deadlock events are rare, small, and valuable for spotting a recurring hot path, so `mon.deadlock_event` is pruned on its own longer horizon (by `deadlock_time_utc`), not the 7-day metric rule.
- **Two prune strategies (config `retention.prune_strategy`):**
  - `delete` (default): **batched** `DELETE TOP (N) ... WHERE collected_at_utc < @cutoff` in a loop (e.g. 50k rows/batch) so the transaction log stays small and no long blocking delete runs. Simple; good at this volume.
  - `partition_switch` (optional, if history ever grows): range-partition the big sample tables by day and **switch out + drop** the expired partition — near-instant, minimal logging. Overkill for 7 days/40 servers but documented for scale.
- **Reclaim/space:** no `VACUUM` (that's SQLite). Instead, keep the repo DB in **SIMPLE recovery** (or scheduled log backups if it must be FULL) so the log doesn't grow during prunes; run a **weekly index rebuild/reorg + stats update on `DBA_Monitoring` itself** (`retention.rebuild_repo_indexes`) so the monitoring store's own indexes stay healthy.
- Parquet exports are independent of this rule (optional `export_retention_days` trims them).
- Prune is idempotent and logged (rows deleted per table into the run log).
- **Repo self-guard:** `DBA_Monitoring` is itself in the monitored fleet (or at least its data/log volumes are tracked), so a filling repo disk raises a normal space alert instead of failing silently. Before each run the collector also checks repo connectivity; if the repository is unreachable the run fails fast and raises a crit operational alert (samples for that run are lost — acceptable, no local queue in v1).

---

## 10. Reporting — the daily HTML report is the dashboard

### 10.1 Structure (`report/html.py`, Jinja2)

`sqlhealthwatch report --date YYYY-MM-DD` renders into `reports/{date}/`:

1. **`index.html` — Fleet rollup** (the morning landing page):
   - Header KPIs: # servers reporting, # unreachable, # crit / # warn findings.
   - **Fleet heat grid**: 40 servers × 6 objective columns (CPU / Mem / IO / Space / Index / Stats), each cell green/amber/red from the worst finding in that category in the last 24h. Click a cell → jump to that server page.
   - **Top offenders** tables: highest sustained CPU, lowest PLE, highest IO latency, lowest free space / soonest days-to-full, most fragmented indexes, most/highest-impact missing indexes, stalest stats, slowest queries fleet-wide, **most deadlocks (24h) and longest blocking chains**.
   - Unreachable-servers list with last-seen time and error.
2. **`server-<name>.html` — per server**: instance meta + uptime (with the "counters since restart" caveat), CPU & PLE & IO latency trend sparklines over the retained window, blocking chains seen in the last day, **deadlocks in the last 24h/7d — victim, objects, and the expandable deadlock graph**, space table with days-to-full projection, index/stats/query recommendation lists (each recommendation ships the **generated, non-executed** T-SQL to copy).

### 10.2 Charts

Self-contained (no external CDN): either base64-embedded matplotlib PNG sparklines or inlined Chart.js. Trend windows come from the 7-day data in `DBA_Monitoring`. Colors follow warn/crit thresholds so the visual severity matches the finding severity.

### 10.3 Excel / CSV export (`report/excel.py`)

`sqlhealthwatch export --date YYYY-MM-DD` produces one `.xlsx` per day: a **Summary** sheet (findings), then one sheet per objective (CPU, Memory, IO, Space, Indexes, Stats, Queries), each a flat table sourced from the Parquet exports. CSVs are the same tables, one file each, for pivoting.

---

## 11. Alerting — `alerting/`

### 11.1 Model

- The **Analyzer** (`analyze/thresholds.py`) evaluates every collected metric against effective thresholds (defaults → tag override → server override) and writes rows to `findings` with `severity` and a stable `fingerprint` (e.g. `server|category|metric|object`).
- The **Router** (`alerting/router.py`) decides what actually pushes: only `warn`/`crit`, deduped by `fingerprint` within a **cooldown window** (e.g. don't re-alert the same finding for 60 min), honoring **quiet hours** and per-tag routing from `alerts.yml`. Fast-tier alerting covers CPU, memory, IO latency, free space, and blocking; index/stats/query findings are digested into the daily report rather than paged.
- Channels: **email (SMTP)**, **Teams (incoming webhook)**, **Slack (incoming webhook)** — all config-driven; v1 can ship with email as the default and webhooks optional. Each alert links to the relevant server HTML page.

### 11.2 `alerts.yml`

```yaml
channels:
  email:
    enabled: true
    smtp_host: smtp.corp.local
    smtp_port: 25
    from: sqlhealthwatch@corp.local
    to: [dba-team@corp.local]
  teams:
    enabled: false
    webhook_ref: env:TEAMS_WEBHOOK
  slack:
    enabled: false
    webhook_ref: env:SLACK_WEBHOOK
routing:
  crit: [email, teams]
  warn: [email]
cooldown_minutes: 60
quiet_hours: { start: "22:00", end: "06:00", allow_crit: true }
by_tag:
  tier1: { routing: { crit: [email, teams, slack] } }
```

### 11.3 Alert catalog (initial)

| Category | Fires when | Severity |
|---|---|---|
| CPU | sustained `sql_cpu_pct` ≥ warn/crit over last N fast samples | warn/crit |
| CPU | `signal_wait_pct` ≥ warn | warn |
| Memory | `page_life_expectancy` < dynamic floor (and < `ple_crit`) | warn/crit |
| Memory | `memory_grants_pending` > 0 sustained | warn |
| IO | interval read/write latency ≥ warn/crit | warn/crit |
| Space | `db_free_pct` or `drive_free_pct` ≤ warn/crit, or days-to-full < 7 | warn/crit |
| Blocking | block chain wait ≥ warn/crit seconds | warn/crit |
| Deadlock | new deadlocks in last 24h ≥ warn/crit count (daily digest; opt-in fast alert) | warn/crit |
| Availability | server unreachable this run | crit |
| Index/Stats/Query | daily digest (report), not paged | info |

---

## 12. Scheduling — two tiers

- **Fast tier — every 15 min:** `sqlhealthwatch run-fast` → collectors tagged `fast` (cpu, memory, io_disk, waits, blocking, space-drive quick check). Lightweight; whole fleet should complete well within the interval given bounded concurrency.
- **Daily tier — once, off-hours (e.g. 06:00 collector-local):** `sqlhealthwatch run-daily` → the heavy collectors (index_frag `LIMITED`, index_missing/unused, stats_age, query_store/plan_cache rollup, db space detail, **deadlocks** from system_health, instance_meta) **then** `report` + `export` + retention `prune`.
- **Production scheduling:** two **Windows Task Scheduler** jobs invoking the CLI (preferred over an always-on process — survives reboots, easy to see last-run/last-result). APScheduler is offered for dev/single-process runs.
- **Overlap guard:** a run acquires a per-tier lock via `sp_getapplock` on the repository (or a `mon.run_lock` row); if the previous run of the same tier is still going, the new one logs-and-skips rather than piling on.
- **Staggering:** the fast tier fans out with bounded concurrency (`max_workers`) and a per-server timeout so one slow/unreachable server can't stall the batch.

---

## 13. Connection, resilience & concurrency — `connection.py`, `runner.py`

- **Connection factory** builds the pyodbc connection string per server from `auth`:
  - `windows`: `DRIVER={ODBC Driver 18 for SQL Server};SERVER=host,port;DATABASE=master;Trusted_Connection=yes;Encrypt=yes;TrustServerCertificate=<cfg>`
  - `sql`: `...;UID=<user>;PWD=<secret>;Encrypt=yes;TrustServerCertificate=<cfg>`
- **Per-server isolation:** each server runs in its own worker; a failure (timeout, login, network) is caught, recorded in `server_status`, and never aborts the run. Unreachable → crit availability alert.
- **Timeouts:** short `connect_timeout` (≈5s) + `query_timeout` (≈30s, longer for daily index scans). Every DMV query is read-only and bounded.
- **Retry:** one quick reconnect retry on transient errors; then mark failed.
- **Least surprise on prod:** fragmentation uses `LIMITED`; no `DETAILED` scans; no query hints that recompile; `SET LOCK_TIMEOUT` low so the monitor never blocks production.

---

## 14. Security

- **Least-privilege principal per monitored instance (read):** `VIEW SERVER STATE`, `VIEW ANY DEFINITION`, `CONNECT`, and `db_datareader`/Query Store read where per-DB queries run. **No sysadmin.** Provisioning script documented as part of onboarding.
- **Repository principal (write):** a separate login on the repo instance mapped to a `DBA_Monitoring` user with **`db_datawriter` + `db_datareader`** (and `db_ddladmin` only for the one-time schema bootstrap, then removed), plus EXECUTE on the insert/TVP procs if used. It has no rights on the 40 monitored boxes; the read principal has no rights on the repo. If the collector runs as an AD service account, the same account can hold both — but they remain distinct grants. Script: `sql/repository/create_repo_login.sql`.
- **Secrets never in YAML:** passwords via `env:` / Windows Credential Manager (`credman:`) / DPAPI (`dpapi:`) refs, resolved at load. `.env` is gitignored. Windows auth is preferred precisely to avoid stored passwords.
- **Transport encryption:** `Encrypt=yes`; move from `TrustServerCertificate=true` to trusted certs in hardening.
- **Collector host** is treated as sensitive (it can read every instance's DMVs): restricted access, audit logging on the service account.
- **No PII/data content** is collected — only metadata, counters, and query *text of the top statements* (which can contain literals; the report offers an option to hash/param-strip statement text if that's a concern).

---

## 15. Deployment & operations

- **Install:** `pip install .` on the Windows collector host (Python 3.11+, MS ODBC Driver 18 installed). Service account = an AD account with the read monitoring login on every instance (covers the `windows` servers) and the write login on the repo; SQL-login servers use per-server secrets.
- **One-time repository setup:** on the chosen repo instance, run `sql/repository/create_database.sql` (creates `DBA_Monitoring` + `mon` schema + tables/indexes) and `create_repo_login.sql`, then `sqlhealthwatch test-conn --repo` to verify write access. `sqlhealthwatch` also self-checks/creates the schema on first run if `repository.auto_bootstrap: true`.
- **Onboarding a server:** add a block to `servers.yml`, run the provisioning script on the instance, `sqlhealthwatch test-conn --server NAME` to verify, done. No code change.
- **CLI surface:**
  - `sqlhealthwatch test-conn [--server X|--all|--repo]` — connectivity + permission check + version/feature report (`--repo` verifies repository write access + schema).
  - `sqlhealthwatch run-fast` / `run-daily`
  - `sqlhealthwatch report [--date]` / `export [--date]`
  - `sqlhealthwatch prune`
- **Observability of the monitor itself:** rotating logs, a `runs`/`server_status` summary line each run, and a self-health section in the report (last run times, failure counts, DB size, disk headroom).

---

## 16. Testing — `tests/`

- **Unit:** `derive.py` rate math (latency, CPU %, days-to-full slope) with fixed sample pairs; `thresholds.py` override precedence (default→tag→server) and severity selection; `router.py` dedup/cooldown/quiet-hours logic; config validation (bad YAML rejected).
- **Fixture-based:** feed captured DMV result sets (JSON fixtures) through each collector's `transform` → assert normalized rows (no live SQL needed for most tests).
- **Integration (optional, gated):** a local SQL Server (container or dev instance) — `test-conn`, one full `run-fast`, schema round-trip, report render smoke test.
- **Query validation across versions:** each `sql/*.sql` (primary **and** legacy variant) is checked to parse and run against the real version matrix in the fleet (e.g. 2008 R2 / 2012 / 2014 / 2016 / 2017 / 2019 / 2022) — ideally via throwaway containers or dev instances per major version — plus a **feature-gate test**: point `version.py` at a simulated pre-2016 / no-SP feature set and assert it selects the legacy SQL and disables `drive_free_pct` alerting.
- **Report render test:** render templates with synthetic data; assert no template errors and that severity coloring maps correctly.

---

## 17. Delivery phases (suggested build order)

1. **Foundation:** config models, connection factory (both auth modes), `test-conn`, version/feature detection, **repository bootstrap** (`create_database.sql` + `mon` schema), logging. → prove you can reach all 40, read DMVs, and write to `DBA_Monitoring`.
2. **Fast tier:** cpu, memory, io_disk, space-drive, waits, blocking collectors + derive + storage. → intraday metrics landing in the repository.
3. **Daily tier:** index, stats, query_store/plan_cache, db-space, instance_meta. → recommendations data.
4. **Analyzer + Alerter:** thresholds, findings, router, email channel (Teams/Slack optional).
5. **Report + Export:** HTML fleet rollup + per-server pages, Excel/CSV.
6. **Retention + scheduling + hardening:** batched prune (+ optional partition switch), weekly repo index maintenance, Task Scheduler jobs, secrets hardening, docs.
7. **(Future) `--apply` mode:** opt-in guarded remediation (reorg/rebuild, update stats) — explicitly out of v1 scope.

---

## 18. Known caveats to honor in the build

- Cumulative DMVs (`dm_os_wait_stats`, `dm_io_virtual_file_stats`, `dm_db_index_usage_stats`, `dm_exec_query_stats`) are **since restart** — always interpret against `sqlserver_start_time`; prefer interval deltas for rates.
- **Plan cache is volatile** (cleared on restart/memory pressure/recompile) — that's why Query Store is the primary query-history source where available.
- **Missing-index DMV over-recommends** and ignores write cost and overlap — treat as ranked suggestions, never auto-apply.
- **PLE 300 is a myth** — scale by buffer pool and read per-NUMA-node.
- **Fragmentation `DETAILED` is expensive** — use `LIMITED`; and rebuild/reorg guidance is a starting heuristic, not a mandate.
- **Azure SQL DB** (if any appear) lacks several server-scoped DMVs — gate those collectors; Managed Instance is closer to box product.
- `index_usage_stats` may reset on index rebuild on some builds — note uptime when calling an index "unused."
- **Mixed versions are the norm here (pre-2016 first-class):** never assume a DMV exists — probe per instance (§2.1, §7.7). On pre-2008 R2 SP1 boxes drive-space is free-MB-only (no %); on pre-2012 SP1 / 2008 R2 SP2 boxes stats modifications are the approximate `rowmodctr`; pre-2016 boxes have no durable query history (plan cache only). Each limitation is badged on the report, not silently hidden.
- **`xp_fixeddrives` is undocumented** (but present on every version) — acceptable read-only fallback; requires EXECUTE grant and returns drive letters, not mount points.

## 19. Open items to confirm before coding

1. **Which instance hosts the `DBA_Monitoring` repository?** (Recommend dedicated/non-critical, separate from the 40; confirm edition/recovery model and that the collector's write login can be created there.)
2. **What is the lowest SQL Server version in the fleet, and the SP levels on pre-2012 boxes?** (Decides how many legacy SQL variants are actually needed — see §2.1. Confirm whether any 2005/2008-RTM instances exist.) Also: any **Azure SQL DB/MI**?
3. Primary **alert channel** for v1 (email default; enable Teams/Slack webhooks?).
4. Collector host OS confirmed **Windows** (needed for AD-integrated auth to the `windows` servers)?
5. Is capturing **top-query statement text** acceptable, or should it be param-stripped/hashed?
6. Exact **daily run window** (off-hours) and whether fast-tier alerting should be silenced during maintenance windows.
