"""Connection string construction and result-set handling.

No live database: pyodbc is only imported inside ``connect``, so everything here works on a host
with no ODBC driver.
"""

from __future__ import annotations

import pytest

from sqlhealthwatch.config import RepositoryConfig, ServerConfig
from sqlhealthwatch.connection import (
    LOCK_TIMEOUT_MS,
    _batch,
    _fetch_dicts,
    _quote_name,
    _short_error,
    build_connection_string,
    redact,
)


class TestConnectionString:
    def test_windows_auth_uses_a_trusted_connection(self):
        server = ServerConfig(name="S1", host="h.corp.local", auth="windows")
        built = build_connection_string(server)
        assert "Trusted_Connection=yes" in built
        assert "UID=" not in built and "PWD=" not in built

    def test_sql_auth_resolves_the_secret_reference(self, monkeypatch):
        monkeypatch.setenv("TEST_PW", "s3cret")
        server = ServerConfig(name="S1", host="h", auth="sql", username="svc",
                              password_ref="env:TEST_PW")
        built = build_connection_string(server)
        assert "UID=svc" in built and "PWD=s3cret" in built

    def test_missing_secret_is_reported_clearly(self, monkeypatch):
        monkeypatch.delenv("ABSENT_PW", raising=False)
        server = ServerConfig(name="S1", host="h", auth="sql", username="svc",
                              password_ref="env:ABSENT_PW")
        from sqlhealthwatch.util.secrets import SecretError

        with pytest.raises(SecretError, match="ABSENT_PW"):
            build_connection_string(server)

    def test_named_instance_is_used_instead_of_a_port(self):
        server = ServerConfig(name="S1", host="h", instance="SQL2019")
        assert "SERVER=h\\SQL2019" in build_connection_string(server)

    def test_encryption_settings_are_honoured(self):
        server = ServerConfig(name="S1", host="h", encrypt=True, trust_server_certificate=False)
        built = build_connection_string(server)
        assert "Encrypt=yes" in built and "TrustServerCertificate=no" in built

    def test_repository_config_builds_the_same_way(self):
        repo = RepositoryConfig(host="repo.test", database="DBA_Monitoring")
        built = build_connection_string(repo, database="DBA_Monitoring")
        assert "DATABASE=DBA_Monitoring" in built
        assert "Trusted_Connection=yes" in built

    def test_application_name_identifies_the_monitor_on_the_instance(self):
        # So a DBA looking at sys.dm_exec_sessions can see who is asking.
        assert "APP=sqlhealthwatch" in build_connection_string(ServerConfig(name="S1", host="h"))


class TestRedaction:
    def test_passwords_never_reach_the_log(self, monkeypatch):
        monkeypatch.setenv("TEST_PW", "s3cret")
        server = ServerConfig(name="S1", host="h", auth="sql", username="svc",
                              password_ref="env:TEST_PW")
        redacted = redact(build_connection_string(server))
        assert "s3cret" not in redacted and "PWD=***" in redacted

    def test_everything_else_survives(self):
        assert "SERVER=h,1433" in redact(build_connection_string(ServerConfig(name="S1", host="h")))


class FakeCursor:
    """Mimics a pyodbc cursor walking multiple result sets."""

    def __init__(self, sets):
        self.sets = list(sets)
        self._advance()

    def _advance(self):
        current = self.sets.pop(0) if self.sets else (None, [])
        self.description, self._rows = current

    def nextset(self):
        if not self.sets:
            return False
        self._advance()
        return True

    def fetchall(self):
        return self._rows


class TestResultSets:
    def test_plain_result_set(self):
        cursor = FakeCursor([([("a",), ("b",)], [(1, 2)])])
        assert _fetch_dicts(cursor) == [{"a": 1, "b": 2}]

    def test_leading_empty_set_from_use_is_skipped(self):
        # "USE [db]; SELECT ..." yields a first result set with no columns; returning [] there
        # would make every per-database collector silently produce nothing.
        cursor = FakeCursor([(None, []), ([("a",)], [(7,)])])
        assert _fetch_dicts(cursor) == [{"a": 7}]

    def test_several_leading_empty_sets_are_skipped(self):
        cursor = FakeCursor([(None, []), (None, []), ([("a",)], [(7,)])])
        assert _fetch_dicts(cursor) == [{"a": 7}]

    def test_a_batch_with_no_rows_at_all_returns_empty(self):
        cursor = FakeCursor([(None, [])])
        assert _fetch_dicts(cursor) == []


class TestBatchPreparation:
    def test_nocount_is_prepended(self):
        # INSERT ... EXEC otherwise hands back a row-count result set first.
        assert _batch("SELECT 1").startswith("SET NOCOUNT ON;\n")

    def test_the_original_query_is_untouched(self):
        assert _batch("SELECT 1").endswith("SELECT 1")


class TestQuoting:
    def test_database_names_are_bracket_quoted(self):
        assert _quote_name("ERP") == "[ERP]"

    def test_embedded_brackets_are_doubled(self):
        assert _quote_name("we[i]rd") == "[we[i]]rd]"


class TestErrorShortening:
    def test_the_driver_message_is_kept(self):
        message = ("('28000', \"[28000] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]"
                   "Login failed for user 'svc'. (18456)\")")
        assert "Login failed for user" in _short_error(Exception(message))

    def test_no_error_is_handled(self):
        assert _short_error(None) == "unknown error"


def test_lock_timeout_is_short_enough_to_never_block_production():
    # The monitor gives up rather than queueing behind a production lock.
    assert LOCK_TIMEOUT_MS <= 10000
