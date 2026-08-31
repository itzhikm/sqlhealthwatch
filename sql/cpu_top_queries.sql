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
