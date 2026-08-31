-- perf_counters_legacy.sql : same counters without the Buffer Node object (pre-2008: no per-NUMA PLE)
SELECT
    RTRIM(counter_name) AS counter_name,
    RTRIM(instance_name) AS instance_name,
    cntr_value
FROM sys.dm_os_performance_counters
WHERE (object_name LIKE '%Buffer Manager%' AND counter_name IN
        ('Page life expectancy','Buffer cache hit ratio','Buffer cache hit ratio base',
         'Free list stalls/sec','Lazy writes/sec','Page reads/sec','Page writes/sec'))
   OR (object_name LIKE '%Memory Manager%' AND counter_name IN
        ('Memory Grants Pending','Memory Grants Outstanding','Total Server Memory (KB)',
         'Target Server Memory (KB)'));
