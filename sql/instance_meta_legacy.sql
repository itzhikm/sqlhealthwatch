-- instance_meta_legacy.sql : 2005 has no sqlserver_start_time on dm_os_sys_info.
-- The login time of SPID 1 is the equivalent instance start time.
SELECT
    (SELECT login_time FROM sys.dm_exec_sessions WHERE session_id = 1)  AS sqlserver_start_time,
    DATEDIFF(MINUTE, (SELECT login_time FROM sys.dm_exec_sessions WHERE session_id = 1), GETDATE()) AS uptime_minutes,
    si.cpu_count,
    si.scheduler_count,
    (SELECT CONVERT(BIGINT, value_in_use) FROM sys.configurations WHERE name = 'max server memory (MB)')        AS max_server_memory_mb,
    (SELECT CONVERT(BIGINT, value_in_use) FROM sys.configurations WHERE name = 'min server memory (MB)')        AS min_server_memory_mb,
    (SELECT CONVERT(INT, value_in_use)    FROM sys.configurations WHERE name = 'max degree of parallelism')     AS maxdop,
    (SELECT CONVERT(INT, value_in_use)    FROM sys.configurations WHERE name = 'cost threshold for parallelism') AS cost_threshold,
    CAST(NULL AS INT)                                                   AS blocked_process_threshold_s
FROM sys.dm_os_sys_info si;
