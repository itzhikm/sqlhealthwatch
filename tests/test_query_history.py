"""Query history: source selection, true per-metric rankings, and no double counting.

The subtle contract here is that ``mon.query_top`` has two writers. The query-history collector owns
duration, reads and executions on every instance, and CPU only on the Query Store path; the CPU
collector owns the plan-cache CPU ranking, which it gets with database attribution. If both wrote
CPU rows on a pre-2016 box the top-CPU list would be duplicated.
"""

from __future__ import annotations

from sqlhealthwatch.collectors.cpu import CpuTopQueriesCollector
from sqlhealthwatch.collectors.plan_cache import RANK_ORDER_BY, sql_file_for
from sqlhealthwatch.collectors.query_store import QueryHistoryCollector
from sqlhealthwatch.version import ServerFeatures


class TestSourceSelection:
    def test_query_store_is_used_when_a_database_has_it_enabled(self, make_context, modern_features):
        ctx = make_context(modern_features)
        assert QueryHistoryCollector().uses_query_store(ctx) is True

    def test_plan_cache_is_used_on_pre_2016(self, make_context, legacy_features):
        ctx = make_context(legacy_features)
        assert QueryHistoryCollector().uses_query_store(ctx) is False

    def test_plan_cache_is_used_when_query_store_is_present_but_off(self, make_context,
                                                                    modern_features):
        for db in modern_features.databases:
            db.is_query_store_on = False
        ctx = make_context(modern_features)
        assert QueryHistoryCollector().uses_query_store(ctx) is False

    def test_legacy_plan_cache_variant_is_chosen_without_query_hash(self, make_context):
        old = ServerFeatures(major_version=9, minor_version=0, engine_edition=3)
        ctx = make_context(old)
        assert sql_file_for(ctx) == "plan_cache_top_legacy.sql"
        assert any("query_hash" in note for note in ctx.notes)


class TestPlanCachePath:
    def test_cpu_ranking_is_left_to_the_cpu_collector(self, make_context, legacy_features,
                                                      fake_connection):
        # Three rankings requested, not four -- the CPU list comes from cpu_top_queries.sql.
        connection = fake_connection([[] for _ in range(4)])
        ctx = make_context(legacy_features, connection)
        QueryHistoryCollector().fetch(ctx)

        ordered = [column for column in RANK_ORDER_BY.values()
                   if any(f"ORDER BY {column} DESC" in q for q in connection.queries)]
        assert "total_cpu_ms" not in ordered
        assert set(ordered) == {"total_duration_ms", "total_logical_reads", "executions"}

    def test_the_volatility_caveat_is_recorded(self, make_context, legacy_features, fake_connection):
        ctx = make_context(legacy_features, fake_connection([[] for _ in range(4)]))
        QueryHistoryCollector().fetch(ctx)
        assert any("not durable" in note for note in ctx.notes)

    def test_each_ranking_is_a_separate_query(self, make_context, legacy_features, fake_connection):
        # Re-sorting one top-25-by-duration would answer a different question.
        connection = fake_connection([[] for _ in range(4)])
        ctx = make_context(legacy_features, connection)
        QueryHistoryCollector().fetch(ctx)
        assert len(connection.queries) == 3


class TestQueryStorePath:
    def test_only_query_store_databases_are_visited(self, make_context, modern_features,
                                                    fake_connection):
        connection = fake_connection([[] for _ in range(10)])
        ctx = make_context(modern_features, connection)
        QueryHistoryCollector().fetch(ctx)

        assert all("USE [ERP]" in query for query in connection.queries)
        assert not any("USE [Archive]" in query for query in connection.queries)

    def test_all_four_rankings_are_collected(self, make_context, modern_features, fake_connection):
        connection = fake_connection([[] for _ in range(10)])
        ctx = make_context(modern_features, connection)
        QueryHistoryCollector().fetch(ctx)
        assert len(connection.queries) == 4

    def test_partial_coverage_is_called_out(self, make_context, modern_features, fake_connection):
        # Query Store on 1 of 2 databases means the other has no durable history at all.
        ctx = make_context(modern_features, fake_connection([[] for _ in range(10)]))
        QueryHistoryCollector().fetch(ctx)
        assert any("1 of 2 databases" in note for note in ctx.notes)


class TestTransform:
    def test_query_store_rows_are_normalized(self, make_context, modern_features):
        rows = [{
            "database_name": "ERP", "query_id": 42, "query_sql_text": "SELECT * FROM Orders",
            "executions": 900, "total_duration_ms": 90000.0, "avg_duration_ms": 100.0,
            "max_duration_ms": 900.0, "total_cpu_ms": 45000.0, "total_logical_reads": 900000,
            "rank_metric": "duration", "source": "query_store",
        }]
        frame = QueryHistoryCollector().transform(rows, make_context(modern_features))
        row = frame.iloc[0]

        assert row["source"] == "query_store"
        assert row["query_identity"] == "42"
        assert row["statement_text"] == "SELECT * FROM Orders"
        assert row["rank_metric"] == "duration"

    def test_plan_cache_rows_use_the_query_hash_as_identity(self, make_context, legacy_features):
        rows = [{
            "database_name": None, "query_identity": "0xABCDEF", "statement_text": "SELECT 1",
            "executions": 10, "total_duration_ms": 500.0, "avg_duration_ms": 50.0,
            "max_duration_ms": 90.0, "total_cpu_ms": 250.0, "total_logical_reads": 1000,
            "rank_metric": "duration", "source": "plan_cache",
        }]
        frame = QueryHistoryCollector().transform(rows, make_context(legacy_features))
        assert frame.iloc[0]["query_identity"] == "0xABCDEF"

    def test_statement_text_can_be_hashed(self, make_context, modern_features, settings):
        settings.collection.statement_text_mode = "hash"
        rows = [{"database_name": "ERP", "query_id": 1, "query_sql_text": "SELECT 'literal'",
                 "executions": 1, "total_duration_ms": 1.0, "avg_duration_ms": 1.0,
                 "max_duration_ms": 1.0, "total_cpu_ms": 1.0, "total_logical_reads": 1,
                 "rank_metric": "duration", "source": "query_store"}]
        frame = QueryHistoryCollector().transform(rows, make_context(modern_features))
        assert frame.iloc[0]["statement_text"].startswith("sha256:")

    def test_each_ranking_keeps_its_own_top_n(self, make_context, modern_features, settings):
        settings.collection.top_n_queries = 2
        rows = [
            {"database_name": "ERP", "query_id": i, "query_sql_text": f"SELECT {i}",
             "executions": i, "total_duration_ms": float(i), "avg_duration_ms": 1.0,
             "max_duration_ms": 1.0, "total_cpu_ms": 1.0, "total_logical_reads": 1,
             "rank_metric": metric, "source": "query_store"}
            for metric in ("duration", "cpu")
            for i in range(5)
        ]
        frame = QueryHistoryCollector().transform(rows, make_context(modern_features))
        assert len(frame[frame["rank_metric"] == "duration"]) == 2
        assert len(frame[frame["rank_metric"] == "cpu"]) == 2


class TestCpuTopQueries:
    def test_rows_are_written_as_the_cpu_ranking(self, make_context, modern_features):
        rows = [{
            "total_cpu_us": 45_000_000, "execution_count": 900, "avg_cpu_us": 50_000,
            "avg_elapsed_us": 100_000, "avg_logical_reads": 1000,
            "last_execution_time": None, "database_name": "ERP",
            "statement_text": "SELECT * FROM Orders", "query_hash": b"\xab\xcd",
            "query_plan_hash": None,
        }]
        frame = CpuTopQueriesCollector().transform(rows, make_context(modern_features))
        row = frame.iloc[0]

        assert row["rank_metric"] == "cpu"
        assert row["source"] == "plan_cache"
        assert row["database_name"] == "ERP"
        assert row["total_cpu_ms"] == 45000.0

    def test_binary_query_hash_is_rendered_readably(self, make_context, modern_features):
        rows = [{"total_cpu_us": 1000, "execution_count": 1, "avg_elapsed_us": 100,
                 "avg_logical_reads": 1, "database_name": "ERP", "statement_text": "SELECT 1",
                 "query_hash": b"\xab\xcd"}]
        frame = CpuTopQueriesCollector().transform(rows, make_context(modern_features))
        assert frame.iloc[0]["query_identity"] == "0xABCD"
