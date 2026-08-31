-- create_repo_login.sql : the collector's WRITE principal on the repository instance.
--
-- This grant is distinct from the read principal on the 40 monitored instances (see
-- create_monitor_login.sql). If the collector runs as an AD service account the same account can
-- hold both, but the grants stay separate: the repo login has no rights on the monitored boxes and
-- the monitoring login has no rights here.
--
-- db_ddladmin is needed only for the one-time schema bootstrap. Drop it afterwards (statement at the
-- bottom) unless repository.auto_bootstrap stays enabled.

USE master;
GO

-- Windows / AD service account (preferred -- no stored password):
IF SUSER_ID('CORP\svc_sqlhealthwatch') IS NULL
    CREATE LOGIN [CORP\svc_sqlhealthwatch] FROM WINDOWS;
GO

-- SQL login alternative (password supplied out of band, never stored in YAML):
-- IF SUSER_ID('svc_dba_monitor_repo') IS NULL
--     CREATE LOGIN svc_dba_monitor_repo WITH PASSWORD = N'<supplied at provisioning time>',
--         CHECK_POLICY = ON;
-- GO

USE DBA_Monitoring;
GO

IF USER_ID('CORP\svc_sqlhealthwatch') IS NULL
    CREATE USER [CORP\svc_sqlhealthwatch] FOR LOGIN [CORP\svc_sqlhealthwatch];
GO

ALTER ROLE db_datawriter ADD MEMBER [CORP\svc_sqlhealthwatch];
ALTER ROLE db_datareader ADD MEMBER [CORP\svc_sqlhealthwatch];
GO

-- Bootstrap only -- creates the mon schema and tables on first run.
ALTER ROLE db_ddladmin ADD MEMBER [CORP\svc_sqlhealthwatch];
GO

-- After `sqlhealthwatch test-conn --repo` confirms the schema exists, revoke DDL rights and set
-- repository.auto_bootstrap: false in settings.yml:
-- ALTER ROLE db_ddladmin DROP MEMBER [CORP\svc_sqlhealthwatch];
-- GO
