-- space_db.sql : used/free space per data & log file. Executed once per database context.
SELECT
    DB_NAME()                              AS database_name,
    f.name                                 AS logical_name,
    f.type_desc                            AS file_type,
    CAST(f.size AS BIGINT) * 8 / 1024      AS size_mb,
    CAST(FILEPROPERTY(f.name,'SpaceUsed') AS BIGINT) * 8 / 1024 AS used_mb,
    (CAST(f.size AS BIGINT) - CAST(FILEPROPERTY(f.name,'SpaceUsed') AS BIGINT)) * 8 / 1024 AS free_mb,
    CASE WHEN f.max_size IN (-1, 268435456) THEN CAST(NULL AS BIGINT)
         ELSE CAST(f.max_size AS BIGINT) * 8 / 1024 END AS max_size_mb,
    f.growth,
    f.is_percent_growth
FROM sys.database_files f;
