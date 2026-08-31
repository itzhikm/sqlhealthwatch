-- databases.sql : per-DB flags used for gating, escalation and the report.
-- is_query_store_on exists only on 2016+, so {query_store_column} is substituted by version.py
-- with either the real column or a constant 0 -- keeping one query instead of two variants for
-- a single column difference.
SELECT
    d.name                          AS database_name,
    d.database_id,
    d.state_desc,
    d.recovery_model_desc,
    d.compatibility_level,
    d.is_read_only,
    d.is_auto_update_stats_on,
    d.is_auto_update_stats_async_on,
    d.is_auto_create_stats_on,
    {query_store_column}            AS is_query_store_on
FROM sys.databases d
WHERE d.state_desc = 'ONLINE'
  AND d.source_database_id IS NULL      -- skip snapshots
ORDER BY d.name;
