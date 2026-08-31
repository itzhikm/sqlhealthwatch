-- cpu_ring_buffer.sql : SQL Server process CPU % history from the scheduler ring buffer.
-- The buffer holds roughly the last 256 minutes as one-minute samples; the newest row is the
-- point-in-time CPU %, and the fast tier captures the trend across runs.
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
