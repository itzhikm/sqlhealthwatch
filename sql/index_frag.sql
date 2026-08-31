-- index_frag.sql : fragmentation for non-trivial indexes (executed per database context).
-- LIMITED scan mode only -- DETAILED is expensive and never run against production.
SELECT
    DB_NAME()                              AS database_name,
    OBJECT_SCHEMA_NAME(ips.object_id)      AS schema_name,
    OBJECT_NAME(ips.object_id)             AS table_name,
    i.name                                 AS index_name,
    ips.index_type_desc                    AS index_type,
    ips.avg_fragmentation_in_percent       AS avg_fragmentation_pct,
    ips.page_count,
    CASE
        WHEN ips.avg_fragmentation_in_percent > 30 THEN 'REBUILD'
        WHEN ips.avg_fragmentation_in_percent >= 15 THEN 'REORGANIZE'
        ELSE 'OK'
    END AS recommendation
FROM sys.dm_db_index_physical_stats(DB_ID(), NULL, NULL, NULL, 'LIMITED') ips
JOIN sys.indexes i ON i.object_id = ips.object_id AND i.index_id = ips.index_id
WHERE ips.page_count >= 1000            -- index.min_page_count
  AND ips.avg_fragmentation_in_percent >= 15
  AND i.index_id > 0                    -- skip heaps
ORDER BY ips.avg_fragmentation_in_percent DESC;
