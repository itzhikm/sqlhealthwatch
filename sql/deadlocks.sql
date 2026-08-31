-- deadlocks.sql : new deadlock graphs from the system_health event_file target (2012+ layout).
-- A deadlock is an event, not a state: the lock monitor kills a victim in milliseconds, so it is
-- essentially never visible to a 15-minute poll and must be read after the fact from a persistent
-- source. The file target is preferred over the ring buffer because it survives ring-buffer
-- rollover, so nothing is missed between daily runs.
-- Parameter: the per-server high-water mark (last ingested deadlock_time_utc).
SELECT
    x.event_data.value('(event/@timestamp)[1]','datetime2')             AS deadlock_time_utc,
    CONVERT(NVARCHAR(MAX), x.event_data.query('event/data[@name="xml_report"]/value/deadlock'))
                                                                        AS deadlock_graph
FROM (
    SELECT CAST(event_data AS XML) AS event_data
    FROM sys.fn_xe_file_target_read_file('system_health*.xel', NULL, NULL, NULL)
    WHERE object_name = 'xml_deadlock_report'
) AS x
WHERE x.event_data.value('(event/@timestamp)[1]','datetime2') > ?
ORDER BY deadlock_time_utc;
