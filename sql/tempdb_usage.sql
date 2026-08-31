-- tempdb_usage.sql : tempdb allocation split (user objects / internal / version store / free)
SELECT
    SUM(unallocated_extent_page_count)      * 8 / 1024 AS free_mb,
    SUM(user_object_reserved_page_count)    * 8 / 1024 AS user_object_mb,
    SUM(internal_object_reserved_page_count)* 8 / 1024 AS internal_object_mb,
    SUM(version_store_reserved_page_count)  * 8 / 1024 AS version_store_mb,
    SUM(total_page_count)                   * 8 / 1024 AS total_mb
FROM tempdb.sys.dm_db_file_space_usage;
