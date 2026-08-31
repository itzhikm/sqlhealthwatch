-- index_usage.sql : nonclustered indexes with maintenance cost and no read benefit (per database).
-- Caveat: dm_db_index_usage_stats resets on restart (and on rebuild in some builds) -- always read
-- alongside instance uptime before calling an index "unused".
SELECT
    DB_NAME()                              AS database_name,
    OBJECT_SCHEMA_NAME(i.object_id)        AS schema_name,
    OBJECT_NAME(i.object_id)               AS table_name,
    i.name                                 AS index_name,
    ISNULL(us.user_seeks,0)   AS user_seeks,
    ISNULL(us.user_scans,0)   AS user_scans,
    ISNULL(us.user_lookups,0) AS user_lookups,
    ISNULL(us.user_updates,0) AS user_updates,
    (ISNULL(us.user_seeks,0) + ISNULL(us.user_scans,0) + ISNULL(us.user_lookups,0)) AS reads,
    us.last_user_seek, us.last_user_scan
FROM sys.indexes i
LEFT JOIN sys.dm_db_index_usage_stats us
       ON us.object_id = i.object_id AND us.index_id = i.index_id AND us.database_id = DB_ID()
WHERE i.type_desc = 'NONCLUSTERED' AND i.is_primary_key = 0 AND i.is_unique_constraint = 0
  AND OBJECTPROPERTY(i.object_id,'IsUserTable') = 1
  AND ISNULL(us.user_updates,0) > 100
  AND (ISNULL(us.user_seeks,0) + ISNULL(us.user_scans,0) + ISNULL(us.user_lookups,0)) = 0
ORDER BY us.user_updates DESC;
