-- index_columns.sql : key + included column lists per nonclustered index, for duplicate detection
-- (recommendations.py compares these sets within a table).
SELECT
    DB_NAME()                         AS database_name,
    OBJECT_SCHEMA_NAME(i.object_id)   AS schema_name,
    OBJECT_NAME(i.object_id)          AS table_name,
    i.name                            AS index_name,
    STUFF((SELECT ',' + c.name
           FROM sys.index_columns ic
           JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
           WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id AND ic.is_included_column = 0
           ORDER BY ic.key_ordinal
           FOR XML PATH('')), 1, 1, '') AS key_columns,
    STUFF((SELECT ',' + c.name
           FROM sys.index_columns ic
           JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
           WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id AND ic.is_included_column = 1
           ORDER BY c.name
           FOR XML PATH('')), 1, 1, '') AS included_columns
FROM sys.indexes i
WHERE i.type_desc = 'NONCLUSTERED'
  AND OBJECTPROPERTY(i.object_id,'IsUserTable') = 1;
