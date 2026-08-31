-- feature_probe.sql : cheap, run once per instance at connect, cached on mon.server, refreshed daily.
--
-- NOTE: parse ProductVersion for the major/minor version. Do NOT use SERVERPROPERTY('ProductMajorVersion')
--       -- that property was only added in SQL Server 2014 SP2 and returns NULL on the older boxes
--       this fleet cares about. ProductVersion always has four dot-parts (a.b.c.d), so PARSENAME(...,4)
--       is the major and PARSENAME(...,3) the minor; 2008 and 2008 R2 share major 10 and differ only
--       by minor (0 vs 50), which matters because dm_os_volume_stats exists on 2008 R2 SP1 but never
--       on plain 2008.
--
-- The version numbers are context and reporting. The OBJECT_ID() probes below are what actually gate
-- the collectors: service-pack level -- not major version -- decides whether dm_db_stats_properties
-- and dm_os_volume_stats are present, so object existence is correct even on unusual patch levels.
SELECT
    CONVERT(NVARCHAR(64), SERVERPROPERTY('ProductVersion'))            AS product_version,
    CONVERT(INT, PARSENAME(CONVERT(varchar(32), SERVERPROPERTY('ProductVersion')), 4)) AS major_version,
    CONVERT(INT, PARSENAME(CONVERT(varchar(32), SERVERPROPERTY('ProductVersion')), 3)) AS minor_version,
    CONVERT(NVARCHAR(16),  SERVERPROPERTY('ProductLevel'))             AS product_level,
    CONVERT(NVARCHAR(64),  SERVERPROPERTY('Edition'))                  AS edition,
    CONVERT(INT,           SERVERPROPERTY('EngineEdition'))            AS engine_edition,
    CONVERT(NVARCHAR(128), SERVERPROPERTY('MachineName'))              AS machine_name,
    CASE WHEN OBJECT_ID('sys.dm_db_stats_properties') IS NOT NULL THEN 1 ELSE 0 END AS has_stats_properties,
    CASE WHEN OBJECT_ID('sys.dm_os_volume_stats')     IS NOT NULL THEN 1 ELSE 0 END AS has_volume_stats,
    CASE WHEN OBJECT_ID('sys.query_store_query')      IS NOT NULL THEN 1 ELSE 0 END AS has_query_store_objects,
    CASE WHEN OBJECT_ID('sys.dm_xe_sessions')         IS NOT NULL THEN 1 ELSE 0 END AS has_extended_events;
