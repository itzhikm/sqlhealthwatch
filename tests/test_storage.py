"""Storage plumbing that is easy to get wrong and invisible until it fails in production."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from sqlhealthwatch.storage.parquet_export import export_frame
from sqlhealthwatch.storage.repository import SCHEMA_FILE, _split_batches, _to_tuples
from sqlhealthwatch.storage.retention import prune_exports


class TestBatchSplitting:
    def test_go_separates_batches(self):
        batches = _split_batches("SELECT 1;\nGO\nSELECT 2;\nGO\n")
        assert batches == ["SELECT 1;", "SELECT 2;"]

    def test_go_inside_a_word_is_not_a_separator(self):
        batches = _split_batches("SELECT 'GOOD';\nGO\nSELECT 2;")
        assert len(batches) == 2 and "GOOD" in batches[0]

    def test_trailing_batch_without_go_is_kept(self):
        assert _split_batches("SELECT 1;") == ["SELECT 1;"]

    def test_the_shipped_schema_splits_cleanly(self):
        script = SCHEMA_FILE.read_text(encoding="utf-8").replace("{schema}", "mon")
        batches = _split_batches(script)
        assert len(batches) > 20
        assert all(batch.strip() for batch in batches)
        assert "{schema}" not in script


class TestSchemaFile:
    def test_every_statement_is_idempotent(self):
        # auto_bootstrap re-applies this on every run where the schema is missing.
        script = SCHEMA_FILE.read_text(encoding="utf-8")
        for batch in _split_batches(script.replace("{schema}", "mon")):
            upper = batch.upper()
            if upper.startswith("CREATE TABLE") or upper.startswith("CREATE CLUSTERED") \
                    or upper.startswith("CREATE UNIQUE") or upper.startswith("CREATE NONCLUSTERED"):
                pytest.fail(f"unguarded DDL batch:\n{batch[:120]}")

    def test_sample_tables_are_clustered_on_time_and_server(self):
        # Retention and 7-day window reads both depend on this being a range scan.
        script = SCHEMA_FILE.read_text(encoding="utf-8")
        assert "CREATE CLUSTERED INDEX cix_cpu_sample ON {schema}.cpu_sample " \
               "(collected_at_utc, server_id)" in script

    def test_deadlocks_are_clustered_on_when_they_happened(self):
        script = SCHEMA_FILE.read_text(encoding="utf-8")
        assert "cix_deadlock_event ON {schema}.deadlock_event (deadlock_time_utc, server_id)" in script

    def test_deadlock_dedup_is_enforced_by_a_unique_index(self):
        script = SCHEMA_FILE.read_text(encoding="utf-8")
        assert "CREATE UNIQUE NONCLUSTERED INDEX ux_deadlock_dedup" in script

    def test_reserved_words_are_bracket_quoted(self):
        script = SCHEMA_FILE.read_text(encoding="utf-8")
        assert "[rows] BIGINT" in script


class TestParameterCoercion:
    def test_nan_becomes_none_for_odbc(self):
        frame = pd.DataFrame([{"a": 1.0, "b": np.nan}])
        assert _to_tuples(frame) == [(1.0, None)]

    def test_pandas_na_becomes_none(self):
        frame = pd.DataFrame([{"a": pd.NA, "b": None}])
        assert _to_tuples(frame) == [(None, None)]

    def test_numpy_scalars_are_unwrapped(self):
        frame = pd.DataFrame({"a": np.array([7], dtype=np.int64)})
        value = _to_tuples(frame)[0][0]
        assert value == 7 and isinstance(value, int)

    def test_timestamps_become_datetimes(self):
        frame = pd.DataFrame([{"t": pd.Timestamp("2026-08-30 06:00:00")}])
        value = _to_tuples(frame)[0][0]
        assert isinstance(value, datetime) and not isinstance(value, pd.Timestamp)


class TestExports:
    def test_frame_is_written_under_date_and_tier(self, tmp_path):
        frame = pd.DataFrame([{"server_id": 1, "value": 10}])
        path = export_frame(frame, tmp_path, "2026-08-30", "fast", "cpu", "abcd1234-0000")

        assert path is not None
        assert path.parent == tmp_path / "2026-08-30" / "fast"
        assert path.name.startswith("cpu_abcd1234")

    def test_empty_frame_writes_nothing(self, tmp_path):
        assert export_frame(pd.DataFrame(), tmp_path, "2026-08-30", "fast", "cpu", "r") is None

    def test_export_failure_never_raises(self, tmp_path, monkeypatch):
        # The repository already has the data; a failed cold export must not fail the run.
        frame = pd.DataFrame([{"a": 1}])

        def explode(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(pd.DataFrame, "to_parquet", explode)
        assert export_frame(frame, tmp_path, "2026-08-30", "fast", "cpu", "r") is None

    def test_csv_is_written_alongside_the_parquet_when_asked(self, tmp_path):
        frame = pd.DataFrame([{"server_id": 1, "value": 10}])
        path = export_frame(frame, tmp_path, "2026-08-30", "fast", "cpu", "r", write_csv=True)
        assert path.with_suffix(".csv").exists()


class TestExportRetention:
    def test_expired_folders_are_removed(self, tmp_path):
        old = tmp_path / (date.today() - timedelta(days=40)).isoformat()
        recent = tmp_path / date.today().isoformat()
        for folder in (old, recent):
            (folder / "fast").mkdir(parents=True)
            (folder / "fast" / "cpu.parquet").write_bytes(b"x")

        removed = prune_exports(tmp_path, days=30)

        assert removed == 1
        assert not old.exists() and recent.exists()

    def test_unrecognized_folders_are_left_alone(self, tmp_path):
        (tmp_path / "notes").mkdir()
        assert prune_exports(tmp_path, days=1) == 0
        assert (tmp_path / "notes").exists()

    def test_disabled_retention_removes_nothing(self, tmp_path):
        (tmp_path / "2000-01-01").mkdir()
        assert prune_exports(tmp_path, days=0) == 0
