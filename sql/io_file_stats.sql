-- io_file_stats.sql : virtual file stats; latency derived as stall/IO
-- Cumulative since restart -- derive.py computes interval rates from two consecutive samples.
SELECT
    DB_NAME(vfs.database_id)               AS database_name,
    mf.type_desc                           AS file_type,      -- ROWS / LOG
    mf.physical_name,
    vfs.num_of_reads,
    vfs.num_of_writes,
    vfs.num_of_bytes_read,
    vfs.num_of_bytes_written,
    vfs.io_stall_read_ms,
    vfs.io_stall_write_ms,
    vfs.io_stall,
    vfs.io_stall_read_ms  / NULLIF(vfs.num_of_reads,0)   AS avg_read_latency_ms,
    vfs.io_stall_write_ms / NULLIF(vfs.num_of_writes,0)  AS avg_write_latency_ms
FROM sys.dm_io_virtual_file_stats(NULL, NULL) AS vfs
JOIN sys.master_files AS mf
  ON mf.database_id = vfs.database_id AND mf.file_id = vfs.file_id;
