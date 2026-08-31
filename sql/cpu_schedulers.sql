-- cpu_schedulers.sql : scheduler context for CPU pressure (runnable tasks now)
SELECT
    COUNT(*)                                    AS online_schedulers,
    SUM(CONVERT(INT, runnable_tasks_count))     AS runnable_tasks_now,
    SUM(CONVERT(INT, current_tasks_count))      AS current_tasks_now,
    SUM(CONVERT(INT, pending_disk_io_count))    AS pending_disk_io
FROM sys.dm_os_schedulers
WHERE status = 'VISIBLE ONLINE' AND is_online = 1;
