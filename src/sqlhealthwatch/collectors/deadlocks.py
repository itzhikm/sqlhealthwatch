"""Deadlocks, read after the fact from the always-on ``system_health`` Extended Events session.

A deadlock is an event, not a state: the lock monitor detects the cycle and kills a victim in
milliseconds, so it is essentially never visible to a 15-minute poll of ``dm_exec_requests``. It has
to be collected from a persistent event source instead -- and every 2008+ box already captures
``xml_deadlock_report`` in system_health, so there is nothing to enable on the monitored servers.

Target selection, in order of preference:

    2012+   event_file  -- survives ring-buffer rollover, so nothing is missed between daily runs
    2008    ring_buffer -- the xml_report node sits one level deeper and the file target is often
                           unreadable on these builds
    no XE   skipped     -- 2005 would need trace flag 1222 plus ERRORLOG scraping; that path is
                           deliberately not built until a 2005 box is confirmed in the fleet

Ingestion is incremental against a per-server high-water mark, and the unique ``dedup_key`` index
makes a re-read idempotent if runs overlap. The full graph XML is stored so a DBA can open the
exact cycle.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from xml.etree import ElementTree

import pandas as pd

from .base import Collector, ServerContext, load_sql

log = logging.getLogger(__name__)

# Anything older than this is not worth re-reading on a first run against a fresh watermark.
EPOCH = datetime(1900, 1, 1)


class DeadlockCollector(Collector):
    name = "deadlocks"
    tier = "daily"
    table = "deadlock_event"

    def applies_to(self, ctx: ServerContext) -> bool:
        if not ctx.features.has_extended_events:
            ctx.note("deadlocks: no Extended Events on this version -- capture skipped")
            return False
        if ctx.features.is_azure_sql_db:
            ctx.note("deadlocks: Azure SQL DB has no system_health session in this form -- skipped")
            return False
        return True

    def sql_file_for(self, ctx: ServerContext) -> str:
        if (ctx.features.major_version or 0) >= 11:
            return "deadlocks.sql"
        return "deadlocks_2008.sql"

    def fetch(self, ctx: ServerContext) -> list[dict]:
        since = ctx.watermarks.get(self.name) or EPOCH
        primary = self.sql_file_for(ctx)
        try:
            return ctx.connection.query(load_sql(ctx.sql_dir, primary), [since])
        except Exception as exc:
            # The event_file target is frequently unreadable (permissions, relocated log directory).
            # The ring buffer is capped and in memory, so it can miss older events -- noted, not hidden.
            log.info("%s: deadlock file target unreadable (%s), falling back to ring buffer", ctx.name, exc)
            ctx.note("deadlocks: event_file target unreadable -- read from the ring buffer (recent events only)")
            try:
                return ctx.connection.query(load_sql(ctx.sql_dir, "deadlocks_ringbuffer.sql"), [since])
            except Exception as inner:
                ctx.note(f"deadlocks: ring buffer unreadable too ({inner}) -- no deadlock data this run")
                return []

    def transform(self, rows: list[dict], ctx: ServerContext) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()
        records = []
        for row in rows:
            graph = row.get("deadlock_graph")
            parsed = parse_deadlock_graph(graph)
            occurred = row.get("deadlock_time_utc")
            records.append(
                {
                    "deadlock_time_utc": occurred,
                    "victim_spid": parsed["victim_spid"],
                    "participant_count": parsed["participant_count"],
                    "database_name": parsed["database_name"],
                    "objects": parsed["objects"],
                    "victim_statement": parsed["victim_statement"],
                    "deadlock_graph": graph,
                    "dedup_key": dedup_key(occurred, parsed["victim_spid"], parsed["objects"]),
                }
            )
        frame = pd.DataFrame(records)
        return frame.drop_duplicates(subset=["dedup_key"])

    def watermark_from(self, frame: pd.DataFrame) -> datetime | None:
        """The newest event ingested, which becomes the next run's lower bound."""
        if frame.empty or "deadlock_time_utc" not in frame:
            return None
        values = pd.to_datetime(frame["deadlock_time_utc"], errors="coerce").dropna()
        return values.max().to_pydatetime() if not values.empty else None


def parse_deadlock_graph(graph: str | None) -> dict:
    """Pull victim, participants, objects and the victim's statement out of a deadlock graph.

    Tolerant by design: graph layout differs between 2008 and 2012+, and a graph that cannot be
    parsed still gets stored -- the raw XML is the durable artifact, the parsed columns are the
    index into it.
    """
    empty = {
        "victim_spid": None,
        "participant_count": None,
        "database_name": None,
        "objects": None,
        "victim_statement": None,
    }
    if not graph:
        return empty

    try:
        root = ElementTree.fromstring(str(graph))
    except ElementTree.ParseError:
        return empty

    deadlock = root if root.tag == "deadlock" else (root.find(".//deadlock") or root)

    processes = deadlock.findall(".//process-list/process")
    victim_ids = [v.get("id") for v in deadlock.findall(".//victim-list/victimProcess")]
    victim = next((p for p in processes if p.get("id") in victim_ids), None)

    objects = sorted(
        {
            name
            for resource in deadlock.findall(".//resource-list/*")
            for name in [resource.get("objectname") or resource.get("indexname")]
            if name
        }
    )

    database_name = None
    if objects:
        # objectname is db.schema.table -- the first part is the database.
        database_name = objects[0].split(".")[0].strip("[]") or None

    return {
        "victim_spid": _int(victim.get("spid")) if victim is not None else None,
        "participant_count": len(processes) or None,
        "database_name": database_name,
        "objects": ", ".join(objects)[:4000] or None,
        "victim_statement": _victim_statement(victim),
    }


def _victim_statement(victim) -> str | None:
    if victim is None:
        return None
    frame = victim.find("./executionStack/frame")
    if frame is not None and (frame.text or "").strip():
        return frame.text.strip()[:4000]
    inputbuf = victim.find("./inputbuf")
    if inputbuf is not None and (inputbuf.text or "").strip():
        return inputbuf.text.strip()[:4000]
    return None


def dedup_key(occurred, victim_spid, objects) -> str:
    """server_id is supplied by the unique index; the key covers time + victim + resources."""
    resource_hash = hashlib.sha1((objects or "").encode("utf-8")).hexdigest()[:16]
    return f"{occurred}|{victim_spid}|{resource_hash}"[:200]


def _int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
