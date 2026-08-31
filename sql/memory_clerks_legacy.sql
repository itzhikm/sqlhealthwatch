-- memory_clerks_legacy.sql : pre-2012 clerks expose single/multi/virtual page columns, not pages_kb
SELECT TOP (10)
    [type] AS clerk_type,
    SUM(single_pages_kb + multi_pages_kb) / 1024 AS pages_mb
FROM sys.dm_os_memory_clerks
GROUP BY [type]
ORDER BY SUM(single_pages_kb + multi_pages_kb) DESC;
