-- deadlocks_ringbuffer.sql : fallback when the system_health event_file target is unreadable.
-- In memory and capped, so events older than the buffer window are lost between runs.
-- Parameter: the per-server high-water mark (last ingested deadlock_time_utc).
WITH xe AS (
    SELECT CAST(xet.target_data AS XML) AS target_xml
    FROM sys.dm_xe_session_targets xet
    JOIN sys.dm_xe_sessions xes ON xes.address = xet.event_session_address
    WHERE xes.name = 'system_health' AND xet.target_name = 'ring_buffer'
)
SELECT evt.value('(@timestamp)[1]','datetime2')          AS deadlock_time_utc,
       CONVERT(NVARCHAR(MAX), evt.query('.'))            AS deadlock_graph
FROM xe
CROSS APPLY target_xml.nodes('//RingBufferTarget/event[@name="xml_deadlock_report"]') AS q(evt)
WHERE evt.value('(@timestamp)[1]','datetime2') > ?
ORDER BY deadlock_time_utc;
