-- memory_clerks.sql : top memory consumers
SELECT TOP (10)
    [type] AS clerk_type,
    SUM(pages_kb) / 1024 AS pages_mb
FROM sys.dm_os_memory_clerks
GROUP BY [type]
ORDER BY SUM(pages_kb) DESC;
