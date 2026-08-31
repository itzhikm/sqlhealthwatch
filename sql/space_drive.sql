-- space_drive.sql : free space on every volume that hosts a DB file (2008 R2 SP1+ / 2012+)
SELECT DISTINCT
    vs.volume_mount_point,
    vs.total_bytes / 1024 / 1024 / 1024                     AS total_gb,
    vs.available_bytes / 1024 / 1024 / 1024                 AS free_gb,
    CAST(vs.available_bytes * 100.0 / NULLIF(vs.total_bytes,0) AS DECIMAL(5,2)) AS free_pct
FROM sys.master_files mf
CROSS APPLY sys.dm_os_volume_stats(mf.database_id, mf.file_id) vs;
