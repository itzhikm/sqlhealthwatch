-- plan_cache_top_legacy.sql : 2005 has no query_hash / query_plan_hash columns, so query identity
-- falls back to sql_handle + statement offsets. Coarser: the same logical statement gets a new
-- identity whenever the batch is recompiled into a different plan handle.
SELECT TOP (25)
    CONVERT(VARCHAR(34), qs.sql_handle, 1) + ':' + CONVERT(VARCHAR(12), qs.statement_start_offset)
                                                              AS query_identity,
    qs.execution_count                                        AS executions,
    qs.total_elapsed_time / 1000.0                            AS total_duration_ms,
    qs.total_elapsed_time / NULLIF(qs.execution_count,0) / 1000.0 AS avg_duration_ms,
    qs.max_elapsed_time / 1000.0                              AS max_duration_ms,
    qs.total_worker_time / 1000.0                             AS total_cpu_ms,
    qs.total_logical_reads,
    qs.creation_time,
    qs.last_execution_time,
    SUBSTRING(st.text, (qs.statement_start_offset/2)+1,
        ((CASE qs.statement_end_offset WHEN -1 THEN DATALENGTH(st.text)
              ELSE qs.statement_end_offset END - qs.statement_start_offset)/2)+1) AS statement_text
FROM sys.dm_exec_query_stats qs
CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
ORDER BY {order_by} DESC;
