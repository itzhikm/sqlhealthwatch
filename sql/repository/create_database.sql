-- create_database.sql : one-time repository setup.
-- Run on the instance chosen to host DBA_Monitoring -- a dedicated / non-critical instance, NOT one
-- of the monitored production boxes, so collection load never touches production and a prod outage
-- never takes monitoring down.
--
-- SIMPLE recovery is deliberate: this is a monitoring store with 7-day retention whose batched
-- retention deletes would otherwise grow the log.

IF DB_ID('DBA_Monitoring') IS NULL
BEGIN
    CREATE DATABASE DBA_Monitoring;
END
GO

ALTER DATABASE DBA_Monitoring SET RECOVERY SIMPLE;
GO

USE DBA_Monitoring;
GO

-- The table DDL itself lives in src/sqlhealthwatch/storage/schema.sql, which the collector applies
-- (and can re-apply idempotently) on first run when repository.auto_bootstrap is true:
--     sqlhealthwatch test-conn --repo
GO
