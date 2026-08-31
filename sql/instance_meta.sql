-- instance_meta.sql : uptime, cores/schedulers, memory and parallelism configuration.
-- Uptime is what makes every cumulative DMV readable ("counters since restart"), so it is
-- captured on the daily pass and on the first fast run after start.
SELECT
    si.sqlserver_start_time,
    DATEDIFF(MINUTE, si.sqlserver_start_time, GETDATE())               AS uptime_minutes,
    si.cpu_count,
    si.scheduler_count,
    (SELECT CONVERT(BIGINT, value_in_use) FROM sys.configurations WHERE name = 'max server memory (MB)')        AS max_server_memory_mb,
    (SELECT CONVERT(BIGINT, value_in_use) FROM sys.configurations WHERE name = 'min server memory (MB)')        AS min_server_memory_mb,
    (SELECT CONVERT(INT, value_in_use)    FROM sys.configurations WHERE name = 'max degree of parallelism')     AS maxdop,
    (SELECT CONVERT(INT, value_in_use)    FROM sys.configurations WHERE name = 'cost threshold for parallelism') AS cost_threshold,
    (SELECT CONVERT(INT, value_in_use)    FROM sys.configurations WHERE name = 'blocked process threshold (s)') AS blocked_process_threshold_s
FROM sys.dm_os_sys_info si;
