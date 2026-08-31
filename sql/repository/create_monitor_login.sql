-- create_monitor_login.sql : the least-privilege READ principal, run once on EACH monitored instance.
--
-- No sysadmin. VIEW SERVER STATE covers the DMVs; VIEW ANY DEFINITION covers sys.indexes /
-- sys.stats metadata; db_datareader in each user database covers the per-database collectors
-- (space, index, stats, Query Store).
--
-- Onboarding a server = add a block to servers.yml, run this script, then
--     sqlhealthwatch test-conn --server NAME

USE master;
GO

-- Windows / AD service account (preferred):
IF SUSER_ID('CORP\svc_sqlhealthwatch') IS NULL
    CREATE LOGIN [CORP\svc_sqlhealthwatch] FROM WINDOWS;
GO

-- SQL login alternative for boxes without AD line-of-sight (password supplied out of band and
-- referenced from servers.yml as password_ref: env:<NAME>, never inlined):
-- IF SUSER_ID('svc_dba_monitor') IS NULL
--     CREATE LOGIN svc_dba_monitor WITH PASSWORD = N'<supplied at provisioning time>',
--         CHECK_POLICY = ON;
-- GO

GRANT VIEW SERVER STATE   TO [CORP\svc_sqlhealthwatch];
GRANT VIEW ANY DEFINITION TO [CORP\svc_sqlhealthwatch];
GRANT VIEW ANY DATABASE   TO [CORP\svc_sqlhealthwatch];
GO

-- Required only on pre-2008 R2 SP1 instances, where drive space falls back to xp_fixeddrives
-- (undocumented but present on every version; returns drive letters, not mount points):
-- GRANT EXECUTE ON master..xp_fixeddrives TO [CORP\svc_sqlhealthwatch];
-- GO

-- Per-database read, needed by the per-database collectors. Repeat for each user database, or run
-- the cursor below.
DECLARE @sql NVARCHAR(MAX) = N'';
SELECT @sql = @sql + N'
USE ' + QUOTENAME(name) + N';
IF USER_ID(''CORP\svc_sqlhealthwatch'') IS NULL
    CREATE USER [CORP\svc_sqlhealthwatch] FOR LOGIN [CORP\svc_sqlhealthwatch];
ALTER ROLE db_datareader ADD MEMBER [CORP\svc_sqlhealthwatch];
'
FROM sys.databases
WHERE state_desc = 'ONLINE'
  AND source_database_id IS NULL
  AND name NOT IN ('tempdb');

EXEC sp_executesql @sql;
GO
