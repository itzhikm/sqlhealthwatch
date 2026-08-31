-- stats_age_legacy.sql : last-updated is exact; rowmodctr is an approximation of modifications.
-- Chosen when sys.dm_db_stats_properties is absent (pre-2008 R2 SP2 / pre-2012 SP1).
-- rowmodctr is per-table (not per-statistic) and deprecated -- the report badges this as an estimate.
SELECT
    DB_NAME()                              AS database_name,
    OBJECT_SCHEMA_NAME(s.object_id)        AS schema_name,
    OBJECT_NAME(s.object_id)               AS table_name,
    s.name                                 AS stats_name,
    STATS_DATE(s.object_id, s.stats_id)    AS last_updated,
    si.rowcnt                              AS [rows],
    si.rowmodctr                           AS modification_counter,
    CAST(si.rowmodctr * 1.0 / NULLIF(si.rowcnt,0) AS DECIMAL(6,3)) AS modification_ratio,
    DATEDIFF(DAY, STATS_DATE(s.object_id, s.stats_id), GETDATE()) AS days_since_update,
    s.no_recompute
FROM sys.stats s
JOIN sys.sysindexes si ON si.id = s.object_id AND si.indid IN (0,1)
JOIN sys.objects o ON o.object_id = s.object_id
WHERE o.is_ms_shipped = 0 AND si.rowcnt >= 1000
  AND ( DATEDIFF(DAY, STATS_DATE(s.object_id, s.stats_id), GETDATE()) >= 7
        OR si.rowmodctr * 1.0 / NULLIF(si.rowcnt,0) >= 0.20 )
ORDER BY modification_ratio DESC, days_since_update DESC;
