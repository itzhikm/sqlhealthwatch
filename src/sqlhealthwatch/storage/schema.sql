-- schema.sql : DBA_Monitoring repository DDL.
--
-- Applied idempotently by storage/repository.py on first run (repository.auto_bootstrap) and safe
-- to re-run. Batches are separated by GO. The literal {schema} is substituted with the configured
-- schema name (default: mon).
--
-- Keys & clustering: each sample table has a BIGINT IDENTITY surrogate PK that is NONCLUSTERED,
-- and a CLUSTERED index on (collected_at_utc, server_id) so 7-day-window reads and
-- retention-by-date pruning are range scans over contiguous data.
--
-- Foreign keys to {schema}.server / {schema}.runs are deliberately omitted on the hot sample tables
-- to keep bulk inserts fast; the collector resolves and caches server_id once per run.

IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = '{schema}')
    EXEC('CREATE SCHEMA {schema}');
GO

-- Dimension: one row per monitored instance (samples key on server_id, not the name)
IF OBJECT_ID('{schema}.server') IS NULL
CREATE TABLE {schema}.server (
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
    feature_flags   NVARCHAR(400) NULL,              -- json: has_volume_stats/has_stats_properties/...
    features_checked_utc DATETIME2(0) NULL,          -- when the probe last ran (refreshed daily)
    is_enabled      BIT NOT NULL DEFAULT 1
);
GO

IF OBJECT_ID('{schema}.runs') IS NULL
CREATE TABLE {schema}.runs (
    run_id         UNIQUEIDENTIFIER NOT NULL PRIMARY KEY,
    tier           VARCHAR(8) NOT NULL,             -- fast | daily
    started_utc    DATETIME2(0) NOT NULL,
    finished_utc   DATETIME2(0) NULL,
    servers_ok     INT NULL,
    servers_failed INT NULL,
    notes          NVARCHAR(1000) NULL
);
GO

IF OBJECT_ID('{schema}.server_status') IS NULL
CREATE TABLE {schema}.server_status (          -- per server per run: reachability + timings
    run_id UNIQUEIDENTIFIER NOT NULL,
    server_id INT NOT NULL,
    ok BIT NOT NULL,
    error NVARCHAR(1000) NULL,
    duration_ms INT NULL,
    collected_at_utc DATETIME2(0) NOT NULL
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'cix_server_status' AND object_id = OBJECT_ID('{schema}.server_status'))
CREATE CLUSTERED INDEX cix_server_status ON {schema}.server_status (collected_at_utc, server_id);
GO

IF OBJECT_ID('{schema}.cpu_sample') IS NULL
CREATE TABLE {schema}.cpu_sample (
    id BIGINT IDENTITY(1,1) NOT NULL,
    run_id UNIQUEIDENTIFIER NOT NULL, server_id INT NOT NULL, collected_at_utc DATETIME2(0) NOT NULL,
    sql_cpu_pct TINYINT, other_process_pct TINYINT, system_idle_pct TINYINT,
    signal_wait_pct DECIMAL(5,2), runnable_tasks INT,
    CONSTRAINT pk_cpu_sample PRIMARY KEY NONCLUSTERED (id)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'cix_cpu_sample' AND object_id = OBJECT_ID('{schema}.cpu_sample'))
CREATE CLUSTERED INDEX cix_cpu_sample ON {schema}.cpu_sample (collected_at_utc, server_id);
GO

IF OBJECT_ID('{schema}.memory_sample') IS NULL
CREATE TABLE {schema}.memory_sample (
    id BIGINT IDENTITY(1,1) NOT NULL,
    run_id UNIQUEIDENTIFIER NOT NULL, server_id INT NOT NULL, collected_at_utc DATETIME2(0) NOT NULL,
    page_life_expectancy INT, ple_dynamic_floor INT, min_node_ple INT,
    memory_grants_pending INT, buffer_cache_hit_ratio DECIMAL(5,2),
    total_server_memory_mb INT, target_server_memory_mb INT,
    CONSTRAINT pk_memory_sample PRIMARY KEY NONCLUSTERED (id)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'cix_memory_sample' AND object_id = OBJECT_ID('{schema}.memory_sample'))
CREATE CLUSTERED INDEX cix_memory_sample ON {schema}.memory_sample (collected_at_utc, server_id);
GO

IF OBJECT_ID('{schema}.io_file_sample') IS NULL
CREATE TABLE {schema}.io_file_sample (
    id BIGINT IDENTITY(1,1) NOT NULL,
    run_id UNIQUEIDENTIFIER NOT NULL, server_id INT NOT NULL, collected_at_utc DATETIME2(0) NOT NULL,
    database_name NVARCHAR(128), file_type VARCHAR(8), physical_name NVARCHAR(260),
    num_of_reads BIGINT, num_of_writes BIGINT, bytes_read BIGINT, bytes_written BIGINT,
    io_stall_read_ms BIGINT, io_stall_write_ms BIGINT,
    avg_read_latency_ms DECIMAL(10,2), avg_write_latency_ms DECIMAL(10,2),          -- since-restart
    interval_read_latency_ms DECIMAL(10,2), interval_write_latency_ms DECIMAL(10,2),-- derived
    interval_read_mb_s DECIMAL(12,2), interval_write_mb_s DECIMAL(12,2),
    CONSTRAINT pk_io_file_sample PRIMARY KEY NONCLUSTERED (id)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'cix_io_file_sample' AND object_id = OBJECT_ID('{schema}.io_file_sample'))
CREATE CLUSTERED INDEX cix_io_file_sample ON {schema}.io_file_sample (collected_at_utc, server_id);
GO

IF OBJECT_ID('{schema}.space_db_sample') IS NULL
CREATE TABLE {schema}.space_db_sample (
    id BIGINT IDENTITY(1,1) NOT NULL,
    run_id UNIQUEIDENTIFIER NOT NULL, server_id INT NOT NULL, collected_at_utc DATETIME2(0) NOT NULL,
    database_name NVARCHAR(128), logical_name NVARCHAR(128), file_type VARCHAR(8),
    size_mb BIGINT, used_mb BIGINT, free_mb BIGINT, free_pct DECIMAL(5,2),
    max_size_mb BIGINT, is_percent_growth BIT,
    CONSTRAINT pk_space_db_sample PRIMARY KEY NONCLUSTERED (id)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'cix_space_db_sample' AND object_id = OBJECT_ID('{schema}.space_db_sample'))
CREATE CLUSTERED INDEX cix_space_db_sample ON {schema}.space_db_sample (collected_at_utc, server_id);
GO

IF OBJECT_ID('{schema}.space_drive_sample') IS NULL
CREATE TABLE {schema}.space_drive_sample (
    id BIGINT IDENTITY(1,1) NOT NULL,
    run_id UNIQUEIDENTIFIER NOT NULL, server_id INT NOT NULL, collected_at_utc DATETIME2(0) NOT NULL,
    volume_mount_point NVARCHAR(260), total_gb DECIMAL(12,2), free_gb DECIMAL(12,2), free_pct DECIMAL(5,2),
    CONSTRAINT pk_space_drive_sample PRIMARY KEY NONCLUSTERED (id)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'cix_space_drive_sample' AND object_id = OBJECT_ID('{schema}.space_drive_sample'))
CREATE CLUSTERED INDEX cix_space_drive_sample ON {schema}.space_drive_sample (collected_at_utc, server_id);
GO

IF OBJECT_ID('{schema}.tempdb_sample') IS NULL
CREATE TABLE {schema}.tempdb_sample (
    id BIGINT IDENTITY(1,1) NOT NULL,
    run_id UNIQUEIDENTIFIER NOT NULL, server_id INT NOT NULL, collected_at_utc DATETIME2(0) NOT NULL,
    total_mb BIGINT, free_mb BIGINT, user_object_mb BIGINT, internal_object_mb BIGINT,
    version_store_mb BIGINT,
    CONSTRAINT pk_tempdb_sample PRIMARY KEY NONCLUSTERED (id)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'cix_tempdb_sample' AND object_id = OBJECT_ID('{schema}.tempdb_sample'))
CREATE CLUSTERED INDEX cix_tempdb_sample ON {schema}.tempdb_sample (collected_at_utc, server_id);
GO

IF OBJECT_ID('{schema}.memory_clerk') IS NULL
CREATE TABLE {schema}.memory_clerk (     -- daily; where the memory actually went
    id BIGINT IDENTITY(1,1) NOT NULL,
    run_id UNIQUEIDENTIFIER NOT NULL, server_id INT NOT NULL, collected_at_utc DATETIME2(0) NOT NULL,
    clerk_type NVARCHAR(128), pages_mb BIGINT,
    CONSTRAINT pk_memory_clerk PRIMARY KEY NONCLUSTERED (id)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'cix_memory_clerk' AND object_id = OBJECT_ID('{schema}.memory_clerk'))
CREATE CLUSTERED INDEX cix_memory_clerk ON {schema}.memory_clerk (collected_at_utc, server_id);
GO

IF OBJECT_ID('{schema}.index_column') IS NULL
CREATE TABLE {schema}.index_column (     -- daily; key/included column sets for duplicate detection
    id BIGINT IDENTITY(1,1) NOT NULL,
    run_id UNIQUEIDENTIFIER NOT NULL, server_id INT NOT NULL, collected_at_utc DATETIME2(0) NOT NULL,
    database_name NVARCHAR(128), schema_name NVARCHAR(128), table_name NVARCHAR(128), index_name NVARCHAR(128),
    key_columns NVARCHAR(MAX), included_columns NVARCHAR(MAX),
    CONSTRAINT pk_index_column PRIMARY KEY NONCLUSTERED (id)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'cix_index_column' AND object_id = OBJECT_ID('{schema}.index_column'))
CREATE CLUSTERED INDEX cix_index_column ON {schema}.index_column (collected_at_utc, server_id);
GO

IF OBJECT_ID('{schema}.index_frag') IS NULL
CREATE TABLE {schema}.index_frag (           -- daily
    id BIGINT IDENTITY(1,1) NOT NULL,
    run_id UNIQUEIDENTIFIER NOT NULL, server_id INT NOT NULL, collected_at_utc DATETIME2(0) NOT NULL,
    database_name NVARCHAR(128), schema_name NVARCHAR(128), table_name NVARCHAR(128), index_name NVARCHAR(128),
    index_type NVARCHAR(60), avg_fragmentation_pct DECIMAL(5,2), page_count BIGINT, recommendation VARCHAR(12),
    CONSTRAINT pk_index_frag PRIMARY KEY NONCLUSTERED (id)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'cix_index_frag' AND object_id = OBJECT_ID('{schema}.index_frag'))
CREATE CLUSTERED INDEX cix_index_frag ON {schema}.index_frag (collected_at_utc, server_id);
GO

IF OBJECT_ID('{schema}.index_missing') IS NULL
CREATE TABLE {schema}.index_missing (        -- daily
    id BIGINT IDENTITY(1,1) NOT NULL,
    run_id UNIQUEIDENTIFIER NOT NULL, server_id INT NOT NULL, collected_at_utc DATETIME2(0) NOT NULL,
    database_name NVARCHAR(128), table_name NVARCHAR(128), avg_user_impact DECIMAL(6,2), demand BIGINT,
    improvement_measure FLOAT, equality_columns NVARCHAR(MAX), inequality_columns NVARCHAR(MAX),
    included_columns NVARCHAR(MAX),
    CONSTRAINT pk_index_missing PRIMARY KEY NONCLUSTERED (id)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'cix_index_missing' AND object_id = OBJECT_ID('{schema}.index_missing'))
CREATE CLUSTERED INDEX cix_index_missing ON {schema}.index_missing (collected_at_utc, server_id);
GO

IF OBJECT_ID('{schema}.index_unused') IS NULL
CREATE TABLE {schema}.index_unused (         -- daily
    id BIGINT IDENTITY(1,1) NOT NULL,
    run_id UNIQUEIDENTIFIER NOT NULL, server_id INT NOT NULL, collected_at_utc DATETIME2(0) NOT NULL,
    database_name NVARCHAR(128), schema_name NVARCHAR(128), table_name NVARCHAR(128), index_name NVARCHAR(128),
    reads BIGINT, user_updates BIGINT, last_user_seek DATETIME2(0) NULL,
    CONSTRAINT pk_index_unused PRIMARY KEY NONCLUSTERED (id)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'cix_index_unused' AND object_id = OBJECT_ID('{schema}.index_unused'))
CREATE CLUSTERED INDEX cix_index_unused ON {schema}.index_unused (collected_at_utc, server_id);
GO

IF OBJECT_ID('{schema}.stats_stale') IS NULL
CREATE TABLE {schema}.stats_stale (          -- daily
    id BIGINT IDENTITY(1,1) NOT NULL,
    run_id UNIQUEIDENTIFIER NOT NULL, server_id INT NOT NULL, collected_at_utc DATETIME2(0) NOT NULL,
    database_name NVARCHAR(128), schema_name NVARCHAR(128), table_name NVARCHAR(128), stats_name NVARCHAR(128),
    last_updated DATETIME2(0) NULL, [rows] BIGINT, modification_counter BIGINT,
    modification_ratio DECIMAL(6,3), days_since_update INT, no_recompute BIT,
    is_estimate BIT NOT NULL DEFAULT 0,      -- 1 when sourced from the legacy rowmodctr path
    CONSTRAINT pk_stats_stale PRIMARY KEY NONCLUSTERED (id)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'cix_stats_stale' AND object_id = OBJECT_ID('{schema}.stats_stale'))
CREATE CLUSTERED INDEX cix_stats_stale ON {schema}.stats_stale (collected_at_utc, server_id);
GO

IF OBJECT_ID('{schema}.query_top') IS NULL
CREATE TABLE {schema}.query_top (            -- daily; source = 'query_store' | 'plan_cache'
    id BIGINT IDENTITY(1,1) NOT NULL,
    run_id UNIQUEIDENTIFIER NOT NULL, server_id INT NOT NULL, collected_at_utc DATETIME2(0) NOT NULL,
    source VARCHAR(12), database_name NVARCHAR(128), query_identity NVARCHAR(64), -- query_id or query_hash
    statement_text NVARCHAR(MAX), executions BIGINT,
    total_duration_ms DECIMAL(18,2), avg_duration_ms DECIMAL(18,2), max_duration_ms DECIMAL(18,2),
    total_cpu_ms DECIMAL(18,2), total_logical_reads BIGINT, rank_metric VARCHAR(10), -- duration|cpu|reads|exec
    CONSTRAINT pk_query_top PRIMARY KEY NONCLUSTERED (id)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'cix_query_top' AND object_id = OBJECT_ID('{schema}.query_top'))
CREATE CLUSTERED INDEX cix_query_top ON {schema}.query_top (collected_at_utc, server_id, rank_metric);
GO

IF OBJECT_ID('{schema}.blocking_event') IS NULL
CREATE TABLE {schema}.blocking_event (       -- fast
    id BIGINT IDENTITY(1,1) NOT NULL,
    run_id UNIQUEIDENTIFIER NOT NULL, server_id INT NOT NULL, collected_at_utc DATETIME2(0) NOT NULL,
    blocked_spid INT, blocking_spid INT, wait_type NVARCHAR(60),
    wait_seconds DECIMAL(10,2), database_name NVARCHAR(128), blocked_stmt NVARCHAR(MAX),
    chain_depth INT NULL,
    CONSTRAINT pk_blocking_event PRIMARY KEY NONCLUSTERED (id)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'cix_blocking_event' AND object_id = OBJECT_ID('{schema}.blocking_event'))
CREATE CLUSTERED INDEX cix_blocking_event ON {schema}.blocking_event (collected_at_utc, server_id);
GO

IF OBJECT_ID('{schema}.deadlock_event') IS NULL
CREATE TABLE {schema}.deadlock_event (       -- daily (from system_health XE); event, not a poll snapshot
    id BIGINT IDENTITY(1,1) NOT NULL,
    run_id UNIQUEIDENTIFIER NOT NULL, server_id INT NOT NULL, collected_at_utc DATETIME2(0) NOT NULL,
    deadlock_time_utc DATETIME2(3) NOT NULL,       -- when the deadlock actually occurred
    victim_spid INT, participant_count INT,
    database_name NVARCHAR(128), objects NVARCHAR(MAX), victim_statement NVARCHAR(MAX),
    deadlock_graph XML NULL,                       -- full graph for drill-down
    dedup_key NVARCHAR(200) NOT NULL,
    CONSTRAINT pk_deadlock_event PRIMARY KEY NONCLUSTERED (id)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'cix_deadlock_event' AND object_id = OBJECT_ID('{schema}.deadlock_event'))
CREATE CLUSTERED INDEX cix_deadlock_event ON {schema}.deadlock_event (deadlock_time_utc, server_id);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ux_deadlock_dedup' AND object_id = OBJECT_ID('{schema}.deadlock_event'))
CREATE UNIQUE NONCLUSTERED INDEX ux_deadlock_dedup ON {schema}.deadlock_event (server_id, dedup_key);
GO

IF OBJECT_ID('{schema}.collector_watermark') IS NULL
CREATE TABLE {schema}.collector_watermark (  -- incremental high-water marks (last ingested deadlock etc.)
    server_id INT NOT NULL, collector VARCHAR(32) NOT NULL,
    last_value_utc DATETIME2(3) NULL, updated_utc DATETIME2(0) NOT NULL,
    CONSTRAINT pk_collector_watermark PRIMARY KEY (server_id, collector)
);
GO

IF OBJECT_ID('{schema}.wait_sample') IS NULL
CREATE TABLE {schema}.wait_sample (          -- fast
    id BIGINT IDENTITY(1,1) NOT NULL,
    run_id UNIQUEIDENTIFIER NOT NULL, server_id INT NOT NULL, collected_at_utc DATETIME2(0) NOT NULL,
    wait_type NVARCHAR(60), wait_time_ms BIGINT, resource_wait_ms BIGINT,
    signal_wait_time_ms BIGINT, waiting_tasks_count BIGINT,
    CONSTRAINT pk_wait_sample PRIMARY KEY NONCLUSTERED (id)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'cix_wait_sample' AND object_id = OBJECT_ID('{schema}.wait_sample'))
CREATE CLUSTERED INDEX cix_wait_sample ON {schema}.wait_sample (collected_at_utc, server_id);
GO

IF OBJECT_ID('{schema}.instance_meta') IS NULL
CREATE TABLE {schema}.instance_meta (        -- daily + first fast run
    id BIGINT IDENTITY(1,1) NOT NULL,
    run_id UNIQUEIDENTIFIER NOT NULL, server_id INT NOT NULL, collected_at_utc DATETIME2(0) NOT NULL,
    sqlserver_start_time DATETIME2(0) NULL, uptime_minutes BIGINT,
    cpu_count INT, scheduler_count INT,
    max_server_memory_mb BIGINT, min_server_memory_mb BIGINT,
    maxdop INT, cost_threshold INT, blocked_process_threshold_s INT,
    database_count INT, auto_update_stats_off_count INT,
    CONSTRAINT pk_instance_meta PRIMARY KEY NONCLUSTERED (id)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'cix_instance_meta' AND object_id = OBJECT_ID('{schema}.instance_meta'))
CREATE CLUSTERED INDEX cix_instance_meta ON {schema}.instance_meta (collected_at_utc, server_id);
GO

IF OBJECT_ID('{schema}.findings') IS NULL
CREATE TABLE {schema}.findings (             -- output of the analyzer
    id BIGINT IDENTITY(1,1) NOT NULL,
    run_id UNIQUEIDENTIFIER NOT NULL, server_id INT NOT NULL, created_utc DATETIME2(0) NOT NULL,
    category VARCHAR(12),          -- cpu|memory|io|space|index|stats|query|blocking|deadlock|availability
    severity VARCHAR(6),           -- info|warn|crit
    metric NVARCHAR(64), observed FLOAT, threshold FLOAT,
    message NVARCHAR(1000), details_json NVARCHAR(MAX), fingerprint NVARCHAR(200), -- dedup key for alerting
    CONSTRAINT pk_findings PRIMARY KEY NONCLUSTERED (id)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'cix_findings' AND object_id = OBJECT_ID('{schema}.findings'))
CREATE CLUSTERED INDEX cix_findings ON {schema}.findings (created_utc, server_id);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_findings_run_sev' AND object_id = OBJECT_ID('{schema}.findings'))
CREATE NONCLUSTERED INDEX ix_findings_run_sev ON {schema}.findings (run_id, severity) INCLUDE (category, metric);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_findings_fingerprint' AND object_id = OBJECT_ID('{schema}.findings'))
CREATE NONCLUSTERED INDEX ix_findings_fingerprint ON {schema}.findings (fingerprint, created_utc); -- alert cooldown lookups
GO

IF OBJECT_ID('{schema}.alert_log') IS NULL
CREATE TABLE {schema}.alert_log (            -- what the router actually pushed (drives cooldown)
    id BIGINT IDENTITY(1,1) NOT NULL,
    server_id INT NOT NULL, fingerprint NVARCHAR(200) NOT NULL, severity VARCHAR(6) NOT NULL,
    channel VARCHAR(16) NOT NULL, sent_utc DATETIME2(0) NOT NULL, ok BIT NOT NULL,
    error NVARCHAR(1000) NULL,
    CONSTRAINT pk_alert_log PRIMARY KEY NONCLUSTERED (id)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'cix_alert_log' AND object_id = OBJECT_ID('{schema}.alert_log'))
CREATE CLUSTERED INDEX cix_alert_log ON {schema}.alert_log (sent_utc, server_id);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_alert_log_fingerprint' AND object_id = OBJECT_ID('{schema}.alert_log'))
CREATE NONCLUSTERED INDEX ix_alert_log_fingerprint ON {schema}.alert_log (fingerprint, sent_utc);
GO
