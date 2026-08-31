-- plan_cache_top.sql : top statements from dm_exec_query_stats.
-- Volatile: the plan cache clears on restart, memory pressure and recompile, so this is a
-- snapshot, not durable history. Day-over-day is diffed snapshot-to-snapshot on query_hash,
-- whose identity is weaker than Query Store's query_id.
-- {order_by} is substituted with one of the ranked metrics; the query is re-run per ranking so
-- each top-N list is a true top-N rather than a re-sort of the first one.
SELECT TOP (25)
    CONVERT(VARCHAR(34), qs.query_hash, 1)                    AS query_identity,
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
