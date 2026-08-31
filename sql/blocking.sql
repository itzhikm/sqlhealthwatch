-- blocking.sql : current blocking chains
SELECT
    r.session_id                           AS blocked_spid,
    r.blocking_session_id                  AS blocking_spid,
    r.wait_type,
    r.wait_time / 1000.0                   AS wait_seconds,
    r.wait_resource,
    DB_NAME(r.database_id)                 AS database_name,
    SUBSTRING(t.text,(r.statement_start_offset/2)+1,
        ((CASE r.statement_end_offset WHEN -1 THEN DATALENGTH(t.text)
              ELSE r.statement_end_offset END - r.statement_start_offset)/2)+1) AS blocked_stmt
FROM sys.dm_exec_requests r
CROSS APPLY sys.dm_exec_sql_text(r.sql_handle) t
WHERE r.blocking_session_id <> 0;
