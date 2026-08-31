"""Test fixtures.

The unit suite runs with no database and no ODBC driver: collectors are exercised through their
``transform`` with captured DMV result sets, and everything else is pure functions over plain data.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sqlhealthwatch.collectors.base import ServerContext  # noqa: E402
from sqlhealthwatch.config import RepositoryConfig, ServerConfig, Settings  # noqa: E402
from sqlhealthwatch.version import DatabaseInfo, ServerFeatures  # noqa: E402


@pytest.fixture
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture
def settings() -> Settings:
    return Settings(
        repository=RepositoryConfig(host="repo.test", database="DBA_Monitoring", schema="mon")
    )


@pytest.fixture
def server() -> ServerConfig:
    return ServerConfig(name="PRD-SQL-01", host="prd-sql-01.test", tags=["tier1", "erp"])


@pytest.fixture
def modern_features() -> ServerFeatures:
    """SQL Server 2019 with everything present and Query Store on one database."""
    return ServerFeatures(
        product_version="15.0.4223.1",
        major_version=15,
        minor_version=0,
        product_level="RTM",
        edition="Enterprise Edition",
        engine_edition=3,
        has_stats_properties=True,
        has_volume_stats=True,
        has_query_store_objects=True,
        has_extended_events=True,
        databases=[
            DatabaseInfo(name="ERP", database_id=5, is_query_store_on=True),
            DatabaseInfo(name="Archive", database_id=6, is_query_store_on=False),
        ],
    )


@pytest.fixture
def legacy_features() -> ServerFeatures:
    """SQL Server 2008 R2 RTM: no volume stats, no stats properties, no Query Store."""
    return ServerFeatures(
        product_version="10.50.1600.1",
        major_version=10,
        minor_version=50,
        product_level="RTM",
        edition="Standard Edition",
        engine_edition=3,
        has_stats_properties=False,
        has_volume_stats=False,
        has_query_store_objects=False,
        has_extended_events=True,
        databases=[DatabaseInfo(name="LEGACY", database_id=5)],
    )


class FakeConnection:
    """Stands in for SqlConnection: returns queued result sets and records what was asked."""

    def __init__(self, results: list[list[dict]] | None = None) -> None:
        self.results = list(results or [])
        self.queries: list[str] = []

    def query(self, sql, params=None, timeout_s=None):
        self.queries.append(sql)
        return self.results.pop(0) if self.results else []

    def query_one(self, sql, params=None, timeout_s=None):
        rows = self.query(sql, params, timeout_s)
        return rows[0] if rows else None

    def query_in_database(self, database, sql, params=None, timeout_s=None):
        return self.query(f"USE [{database}];\n{sql}", params, timeout_s)


@pytest.fixture
def make_context(settings, server, project_root):
    def _make(features, connection=None, tier="fast"):
        return ServerContext(
            server=server,
            features=features,
            connection=connection or FakeConnection(),
            settings=settings,
            sql_dir=project_root / "sql",
            run_id="11111111-2222-3333-4444-555555555555",
            server_id=1,
            collected_at_utc=datetime(2026, 8, 30, 6, 0, 0),
            tier=tier,
        )

    return _make


@pytest.fixture
def fake_connection():
    return FakeConnection
