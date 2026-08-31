"""Feature gating: what a probe result implies, and what gets badged when it is missing."""

from __future__ import annotations

from sqlhealthwatch.version import (
    DatabaseInfo,
    ServerFeatures,
    databases_sql,
    parse_database_rows,
    parse_probe_row,
)


class TestProbeParsing:
    def test_version_is_parsed_from_product_version(self):
        # ProductMajorVersion is NULL before 2014 SP2, which is why the parts are parsed instead.
        features = parse_probe_row(
            {"product_version": "10.50.6000.34", "major_version": 10, "minor_version": 50,
             "product_level": "SP3", "edition": "Standard Edition", "engine_edition": 3,
             "has_stats_properties": 1, "has_volume_stats": 1,
             "has_query_store_objects": 0, "has_extended_events": 1}
        )
        assert features.major_version == 10 and features.minor_version == 50
        assert features.is_2008_r2_or_later

    def test_2008_and_2008_r2_are_distinguished_by_the_minor_version(self):
        # They share major 10, but dm_os_volume_stats exists on R2 SP1 and never on plain 2008.
        plain_2008 = ServerFeatures(major_version=10, minor_version=0)
        r2 = ServerFeatures(major_version=10, minor_version=50)
        assert not plain_2008.is_2008_r2_or_later
        assert r2.is_2008_r2_or_later

    def test_probe_flags_are_booleans(self):
        features = parse_probe_row({"has_volume_stats": 0, "has_stats_properties": 1})
        assert features.has_volume_stats is False
        assert features.has_stats_properties is True


class TestDerivedCapabilities:
    def test_query_store_needs_both_the_version_and_the_objects(self):
        # A 2016+ box with the catalog views missing is still not a Query Store box.
        assert ServerFeatures(major_version=15, has_query_store_objects=True).supports_query_store
        assert not ServerFeatures(major_version=15, has_query_store_objects=False).supports_query_store
        assert not ServerFeatures(major_version=12, has_query_store_objects=True).supports_query_store

    def test_query_store_databases_are_only_the_enabled_ones(self):
        features = ServerFeatures(
            major_version=15,
            has_query_store_objects=True,
            databases=[
                DatabaseInfo(name="ERP", database_id=5, is_query_store_on=True),
                DatabaseInfo(name="Archive", database_id=6, is_query_store_on=False),
            ],
        )
        assert [db.name for db in features.query_store_databases] == ["ERP"]

    def test_query_hash_is_2008_plus(self):
        assert ServerFeatures(major_version=10).has_query_hash
        assert not ServerFeatures(major_version=9).has_query_hash

    def test_azure_sql_db_is_not_the_box_product(self):
        assert ServerFeatures(engine_edition=5).is_azure_sql_db
        assert not ServerFeatures(engine_edition=5).is_box_product
        # Managed Instance behaves like the box product.
        assert ServerFeatures(engine_edition=8).is_box_product

    def test_version_name_reads_naturally(self):
        assert ServerFeatures(major_version=10, minor_version=50,
                              product_level="SP3").version_name == "SQL Server 2008 R2 SP3"
        assert ServerFeatures(major_version=15,
                              product_level="RTM").version_name == "SQL Server 2019 RTM"


class TestLimitations:
    def test_a_fully_featured_instance_has_nothing_to_badge(self, modern_features):
        assert modern_features.limitations() == []

    def test_legacy_instance_explains_every_degraded_path(self, legacy_features):
        notes = " ".join(legacy_features.limitations())
        assert "drive free %" in notes
        assert "estimate" in notes
        assert "plan cache" in notes

    def test_query_store_present_but_unused_is_called_out(self):
        features = ServerFeatures(
            major_version=15, has_query_store_objects=True, has_stats_properties=True,
            has_volume_stats=True, has_extended_events=True,
            databases=[DatabaseInfo(name="ERP", database_id=5, is_query_store_on=False)],
        )
        notes = " ".join(features.limitations())
        assert "not enabled on any database" in notes

    def test_flags_json_records_what_the_report_needs(self, legacy_features):
        import json

        flags = json.loads(legacy_features.flags_json())
        assert flags["has_volume_stats"] is False
        assert flags["has_query_store"] is False


class TestDatabasesSql:
    def test_query_store_column_is_real_on_2016_plus(self, modern_features):
        sql = databases_sql(modern_features, "SELECT {query_store_column} AS is_query_store_on")
        assert "d.is_query_store_on" in sql

    def test_query_store_column_is_a_constant_on_older_versions(self, legacy_features):
        # The result shape stays identical so the caller needs no branch.
        sql = databases_sql(legacy_features, "SELECT {query_store_column} AS is_query_store_on")
        assert "CAST(0 AS BIT)" in sql
        assert "{query_store_column}" not in sql

    def test_rows_are_parsed_into_database_info(self):
        rows = [{"database_name": "ERP", "database_id": 5, "recovery_model_desc": "FULL",
                 "compatibility_level": 150, "is_read_only": 0, "is_auto_update_stats_on": 1,
                 "is_auto_update_stats_async_on": 0, "is_auto_create_stats_on": 1,
                 "is_query_store_on": 1}]
        databases = parse_database_rows(rows)
        assert databases[0].name == "ERP"
        assert databases[0].is_query_store_on is True
        assert databases[0].is_read_only is False
