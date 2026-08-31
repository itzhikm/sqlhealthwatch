-- space_drive_legacy.sql : free MB per drive letter (no total, no %) -- pre-2008 R2 SP1.
-- xp_fixeddrives is undocumented but present on every version; needs EXECUTE for the monitor login.
CREATE TABLE #fixeddrives (drive CHAR(1), free_mb INT);
INSERT INTO #fixeddrives EXEC master..xp_fixeddrives;
SELECT drive AS volume_mount_point,
       CAST(NULL AS DECIMAL(12,2)) AS total_gb,
       free_mb / 1024.0            AS free_gb,
       CAST(NULL AS DECIMAL(5,2))  AS free_pct
FROM #fixeddrives;
DROP TABLE #fixeddrives;
