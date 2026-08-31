-- deadlocks_2008.sql : 2008 / 2008 R2 ring buffer variant.
-- system_health exists on 2008+, but the xml_report node sits under the event's data element
-- rather than being addressable as event/data/value/deadlock the way it is on 2012+, and the
-- file target is often not readable on these boxes -- so the ring buffer is the practical source.
-- Parameter: the per-server high-water mark (last ingested deadlock_time_utc).
WITH xe AS (
    SELECT CAST(xet.target_data AS XML) AS target_xml
    FROM sys.dm_xe_session_targets xet
    JOIN sys.dm_xe_sessions xes ON xes.address = xet.event_session_address
    WHERE xes.name = 'system_health' AND xet.target_name = 'ring_buffer'
)
SELECT evt.value('(@timestamp)[1]','datetime2')                             AS deadlock_time_utc,
       CONVERT(NVARCHAR(MAX), evt.query('data[@name="xml_report"]/value/*')) AS deadlock_graph
FROM xe
CROSS APPLY target_xml.nodes('//RingBufferTarget/event[@name="xml_deadlock_report"]') AS q(evt)
WHERE evt.value('(@timestamp)[1]','datetime2') > ?
ORDER BY deadlock_time_utc;
