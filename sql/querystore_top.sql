-- querystore_top.sql : top queries over a window (executed per database where Query Store is ON).
-- Durable across restarts, so day-over-day comparison keys on the stable query_id.
-- Parameter: window length in hours (collection.query_window_hours).
-- {order_by} is substituted with one of the ranked metrics; the same shape is re-run per
-- ranking so each top-N list is a true top-N.
DECLARE @hours INT = ?;
DECLARE @from DATETIME2 = DATEADD(HOUR, -@hours, SYSUTCDATETIME());
SELECT TOP (25)
    DB_NAME()                                                  AS database_name,
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
ORDER BY {order_by} DESC;
