"""Per-instance version detection and feature gating.

Called once per instance at connect time. Nothing is assumed present: the probe asks the instance
what objects actually exist rather than trusting a version-to-feature table, because service-pack
level -- not major version -- decides whether ``dm_db_stats_properties`` (2008 R2 SP2 / 2012 SP1)
and ``dm_os_volume_stats`` (2008 R2 SP1) are there. That makes the result correct even on odd patch
levels.

Every collector keys its ``applies_to()`` and its SQL-variant choice off the resulting
:class:`ServerFeatures`. Where a fallback loses fidelity, :meth:`ServerFeatures.limitations`
supplies the badge text the report shows so a reading is never silently misinterpreted.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field

log = logging.getLogger(__name__)

# EngineEdition values that are not the box product.
ENGINE_AZURE_SQL_DB = 5
ENGINE_MANAGED_INSTANCE = 8

MAJOR_2005, MAJOR_2008, MAJOR_2012, MAJOR_2014, MAJOR_2016 = 9, 10, 11, 12, 13


@dataclass
class DatabaseInfo:
    """One online, non-snapshot database, with the flags the per-database collectors gate on."""

    name: str
    database_id: int
    recovery_model: str | None = None
    compatibility_level: int | None = None
    is_read_only: bool = False
    is_auto_update_stats_on: bool = True
    is_auto_update_stats_async_on: bool = False
    is_auto_create_stats_on: bool = True
    is_query_store_on: bool = False


@dataclass
class ServerFeatures:
    """Resolved capability set for one instance."""

    product_version: str | None = None
    major_version: int | None = None
    minor_version: int | None = None
    product_level: str | None = None
    edition: str | None = None
    engine_edition: int | None = None
    machine_name: str | None = None

    # Probed directly -- these are what actually gate the collectors.
    has_stats_properties: bool = False
    has_volume_stats: bool = False
    has_query_store_objects: bool = False
    has_extended_events: bool = False

    databases: list[DatabaseInfo] = field(default_factory=list)

    # ------------------------------------------------------------------ derived capabilities

    @property
    def is_azure_sql_db(self) -> bool:
        """Azure SQL DB lacks several server-scoped DMVs, so those collectors are gated off."""
        return self.engine_edition == ENGINE_AZURE_SQL_DB

    @property
    def is_managed_instance(self) -> bool:
        return self.engine_edition == ENGINE_MANAGED_INSTANCE

    @property
    def is_box_product(self) -> bool:
        return not self.is_azure_sql_db

    @property
    def is_2008_r2_or_later(self) -> bool:
        major, minor = self.major_version or 0, self.minor_version or 0
        return major > MAJOR_2008 or (major == MAJOR_2008 and minor >= 50)

    @property
    def has_query_hash(self) -> bool:
        """query_hash / query_plan_hash arrived in 2008; on 2005 identity is sql_handle + offsets."""
        return (self.major_version or 0) >= MAJOR_2008

    @property
    def has_buffer_node_ple(self) -> bool:
        """Per-NUMA-node PLE (the `Buffer Node` counter object) is 2008+."""
        return (self.major_version or 0) >= MAJOR_2008

    @property
    def has_ring_buffer_cpu(self) -> bool:
        """The SCHEDULER_MONITOR ring buffer carries ProcessUtilization from 2008 onward."""
        return (self.major_version or 0) >= MAJOR_2008

    @property
    def has_sys_info_start_time(self) -> bool:
        """dm_os_sys_info.sqlserver_start_time is 2008+; 2005 uses SPID 1's login time."""
        return (self.major_version or 0) >= MAJOR_2008

    @property
    def has_pages_kb_clerks(self) -> bool:
        """dm_os_memory_clerks.pages_kb replaced the single/multi page columns in 2012."""
        return (self.major_version or 0) >= MAJOR_2012

    @property
    def supports_query_store(self) -> bool:
        """Query Store needs 2016+ *and* the catalog views to actually be present."""
        return (self.major_version or 0) >= MAJOR_2016 and self.has_query_store_objects

    @property
    def query_store_databases(self) -> list[DatabaseInfo]:
        """Databases where the durable path applies; everything else takes the plan cache."""
        if not self.supports_query_store:
            return []
        return [d for d in self.databases if d.is_query_store_on]

    @property
    def version_name(self) -> str:
        names = {9: "2005", 10: "2008", 11: "2012", 12: "2014", 13: "2016", 14: "2017", 15: "2019", 16: "2022"}
        major = self.major_version or 0
        if major == MAJOR_2008 and (self.minor_version or 0) >= 50:
            base = "2008 R2"
        else:
            base = names.get(major, f"major {major}")
        return f"SQL Server {base} {self.product_level or ''}".strip()

    # ------------------------------------------------------------------------------ reporting

    def limitations(self) -> list[str]:
        """Badge text for the per-server report page: where this instance is running degraded.

        A fallback is never silently hidden -- if free % is unavailable, the page says so rather
        than showing a blank column that reads as "fine".
        """
        notes: list[str] = []
        if not self.has_volume_stats:
            notes.append(
                "drive free % unavailable on this version (no sys.dm_os_volume_stats) "
                "-- showing free MB from xp_fixeddrives, alerting on absolute MB"
            )
        if not self.has_stats_properties:
            notes.append(
                "statistics modifications are an estimate (sys.sysindexes.rowmodctr is per-table, "
                "not per-statistic) -- last-updated date is exact"
            )
        if not self.supports_query_store:
            notes.append(
                "query history from the plan cache -- not durable, cleared on restart, "
                "memory pressure and recompile"
            )
        elif not self.query_store_databases:
            notes.append("Query Store present but not enabled on any database -- using the plan cache")
        if not self.has_buffer_node_ple:
            notes.append("per-NUMA-node PLE unavailable -- showing instance-level PLE only")
        if not self.has_extended_events:
            notes.append("no Extended Events -- deadlock capture unavailable on this instance")
        if not self.has_query_hash:
            notes.append("no query_hash on this version -- query identity is coarser (sql_handle + offset)")
        if self.is_azure_sql_db:
            notes.append("Azure SQL DB -- server-scoped collectors (CPU ring buffer, drives, waits) are skipped")
        return notes

    # ------------------------------------------------------------------------------- storage

    def flags_json(self) -> str:
        """Compact flag set persisted on mon.server so the report can render without re-probing."""
        return json.dumps(
            {
                "has_volume_stats": self.has_volume_stats,
                "has_stats_properties": self.has_stats_properties,
                "has_query_store": self.supports_query_store,
                "has_extended_events": self.has_extended_events,
                "has_query_hash": self.has_query_hash,
                "has_buffer_node_ple": self.has_buffer_node_ple,
                "query_store_databases": len(self.query_store_databases),
            }
        )

    def as_dict(self) -> dict:
        data = asdict(self)
        data.pop("databases", None)
        return data


def parse_probe_row(row: dict) -> ServerFeatures:
    """Build a feature set from one ``feature_probe.sql`` row."""
    return ServerFeatures(
        product_version=row.get("product_version"),
        major_version=_as_int(row.get("major_version")),
        minor_version=_as_int(row.get("minor_version")),
        product_level=row.get("product_level"),
        edition=row.get("edition"),
        engine_edition=_as_int(row.get("engine_edition")),
        machine_name=row.get("machine_name"),
        has_stats_properties=bool(row.get("has_stats_properties")),
        has_volume_stats=bool(row.get("has_volume_stats")),
        has_query_store_objects=bool(row.get("has_query_store_objects")),
        has_extended_events=bool(row.get("has_extended_events")),
    )


def parse_database_rows(rows: list[dict]) -> list[DatabaseInfo]:
    return [
        DatabaseInfo(
            name=row["database_name"],
            database_id=_as_int(row.get("database_id")) or 0,
            recovery_model=row.get("recovery_model_desc"),
            compatibility_level=_as_int(row.get("compatibility_level")),
            is_read_only=bool(row.get("is_read_only")),
            is_auto_update_stats_on=bool(row.get("is_auto_update_stats_on")),
            is_auto_update_stats_async_on=bool(row.get("is_auto_update_stats_async_on")),
            is_auto_create_stats_on=bool(row.get("is_auto_create_stats_on")),
            is_query_store_on=bool(row.get("is_query_store_on")),
        )
        for row in rows
    ]


def databases_sql(features: ServerFeatures, template: str) -> str:
    """Fill the one version-dependent column in databases.sql.

    ``is_query_store_on`` exists only on 2016+; older instances get a constant so the result shape
    stays identical and the caller needs no branch.
    """
    column = "d.is_query_store_on" if features.supports_query_store else "CAST(0 AS BIT)"
    return template.replace("{query_store_column}", column)


def _as_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
