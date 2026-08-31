-- stats_age.sql : statistics age + modification counter (per database).
-- sys.dm_db_stats_properties requires 2008 R2 SP2 / 2012 SP1+ -- see stats_age_legacy.sql.
SELECT
    DB_NAME()                              AS database_name,
    OBJECT_SCHEMA_NAME(s.object_id)        AS schema_name,
    OBJECT_NAME(s.object_id)               AS table_name,
    s.name                                 AS stats_name,
    sp.last_updated,
    sp.rows                                AS [rows],
    sp.rows_sampled,
    sp.modification_counter,
    CAST(sp.modification_counter * 1.0 / NULLIF(sp.rows,0) AS DECIMAL(6,3)) AS modification_ratio,
    DATEDIFF(DAY, sp.last_updated, GETDATE()) AS days_since_update,
    s.auto_created, s.user_created, s.no_recompute
FROM sys.stats s
CROSS APPLY sys.dm_db_stats_properties(s.object_id, s.stats_id) sp
JOIN sys.objects o ON o.object_id = s.object_id
WHERE o.is_ms_shipped = 0
  AND sp.rows >= 1000
  AND ( DATEDIFF(DAY, sp.last_updated, GETDATE()) >= 7
        OR sp.modification_counter * 1.0 / NULLIF(sp.rows,0) >= 0.20 )
ORDER BY modification_ratio DESC, days_since_update DESC;
