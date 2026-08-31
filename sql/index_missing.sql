-- index_missing.sql : missing indexes ranked by estimated impact.
-- Suggestions only: the DMV over-recommends and ignores write cost and index overlap.
SELECT TOP (50)
    DB_NAME(mid.database_id)               AS database_name,
    OBJECT_NAME(mid.object_id, mid.database_id) AS table_name,
    migs.avg_user_impact,
    migs.user_seeks + migs.user_scans      AS demand,
    migs.avg_total_user_cost,
    (migs.user_seeks + migs.user_scans) * migs.avg_total_user_cost * (migs.avg_user_impact/100.0)
                                           AS improvement_measure,
    mid.equality_columns, mid.inequality_columns, mid.included_columns
FROM sys.dm_db_missing_index_group_stats migs
JOIN sys.dm_db_missing_index_groups mig ON migs.group_handle = mig.index_group_handle
JOIN sys.dm_db_missing_index_details mid ON mig.index_handle = mid.index_handle
ORDER BY improvement_measure DESC;
