"""Connection factory and the thin query wrapper every collector uses.

Two auth modes, selected per server in ``servers.yml``:

    windows -- Trusted_Connection=yes, the collector's AD service account
    sql     -- UID/PWD, password resolved from a secret reference at load time

Session settings are set on every connection so the monitor can never block production: a low
``LOCK_TIMEOUT`` means a metadata read gives up rather than waiting behind a production lock, and
READ UNCOMMITTED keeps the monitor from taking shared locks of its own.

``pyodbc`` is imported lazily so the package (and the unit test suite) stays importable on a host
without the ODBC driver installed.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable, Protocol, Sequence

log = logging.getLogger(__name__)

# Short enough that a monitoring query never queues behind production work.
LOCK_TIMEOUT_MS = 5000


class ConnectionError_(RuntimeError):
    """Raised when an instance cannot be reached or authenticated."""


class _Connectable(Protocol):
    """The subset of config a connection string needs -- ServerConfig and RepositoryConfig both fit."""

    address: str
    auth: str
    username: str | None
    password_ref: str | None
    driver: str
    encrypt: bool
    trust_server_certificate: bool
    connect_timeout_s: int
    query_timeout_s: int


def build_connection_string(cfg: _Connectable, database: str = "master") -> str:
    parts = [
        f"DRIVER={{{cfg.driver}}}",
        f"SERVER={cfg.address}",
        f"DATABASE={database}",
        f"Encrypt={'yes' if cfg.encrypt else 'no'}",
        f"TrustServerCertificate={'yes' if cfg.trust_server_certificate else 'no'}",
        f"Connection Timeout={cfg.connect_timeout_s}",
        "APP=sqlhealthwatch",
    ]
    if cfg.auth == "windows":
        parts.append("Trusted_Connection=yes")
    else:
        from .util import secrets

        parts.append(f"UID={cfg.username}")
        parts.append(f"PWD={secrets.resolve(cfg.password_ref)}")
    return ";".join(parts)


def redact(connection_string: str) -> str:
    """Connection strings reach the log only through here."""
    return ";".join(
        "PWD=***" if part.upper().startswith("PWD=") else part for part in connection_string.split(";")
    )


class SqlConnection:
    """A live connection to one instance, returning rows as plain dicts.

    Collectors receive dicts rather than pyodbc rows so their ``transform`` can be unit tested
    against captured JSON fixtures with no database involved.
    """

    def __init__(self, raw_connection: Any, cfg: _Connectable, database: str = "master") -> None:
        self._conn = raw_connection
        self.cfg = cfg
        self.database = database

    # ----------------------------------------------------------------------------- querying

    def query(self, sql: str, params: Sequence[Any] | None = None, timeout_s: int | None = None) -> list[dict]:
        self._set_timeout(timeout_s)
        cursor = self._conn.cursor()
        try:
            cursor.execute(_batch(sql), *(params or ()))
            return _fetch_dicts(cursor)
        finally:
            cursor.close()

    def query_one(self, sql: str, params: Sequence[Any] | None = None, timeout_s: int | None = None) -> dict | None:
        rows = self.query(sql, params, timeout_s)
        return rows[0] if rows else None

    def query_in_database(
        self, database: str, sql: str, params: Sequence[Any] | None = None, timeout_s: int | None = None
    ) -> list[dict]:
        """Run a per-database query in that database's context.

        USE is issued on the same connection instead of opening one connection per database --
        at 40 servers times N databases that difference matters. The database name is quoted, and
        it comes from ``sys.databases`` on the instance itself, never from user input.
        """
        return self.query(f"USE {_quote_name(database)};\n{sql}", params, timeout_s)

    def execute(self, sql: str, params: Sequence[Any] | None = None, timeout_s: int | None = None) -> int:
        self._set_timeout(timeout_s)
        cursor = self._conn.cursor()
        try:
            cursor.execute(sql, *(params or ()))
            return cursor.rowcount
        finally:
            cursor.close()

    def _set_timeout(self, timeout_s: int | None) -> None:
        """Query timeout, in seconds.

        It lives on the *connection* in pyodbc -- ``Cursor`` has no ``timeout`` attribute, and
        setting one there is silently useless at best (it raised AttributeError here). This is what
        bounds a DMV read so the collector cannot hang on a busy production instance.
        """
        self._conn.timeout = timeout_s if timeout_s is not None else self.cfg.query_timeout_s

    # -------------------------------------------------------------------------- lifecycle

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # pragma: no cover - closing a dead connection is not interesting
            pass

    def __enter__(self) -> "SqlConnection":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def connect(cfg: _Connectable, database: str = "master", retries: int = 1) -> SqlConnection:
    """Open a connection, retrying once on a transient failure before giving up.

    A failure here is not fatal to the run: the caller records it against the server and moves on,
    so one unreachable instance never stalls the batch.
    """
    try:
        import pyodbc
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ConnectionError_(
            "pyodbc is not installed. The collector host needs pyodbc and the "
            "Microsoft ODBC Driver 18 for SQL Server."
        ) from exc

    connection_string = build_connection_string(cfg, database)
    last_error: Exception | None = None

    for attempt in range(retries + 1):
        try:
            raw = pyodbc.connect(connection_string, timeout=cfg.connect_timeout_s, autocommit=True)
            cursor = raw.cursor()
            cursor.execute(
                f"SET LOCK_TIMEOUT {LOCK_TIMEOUT_MS}; "
                "SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED; "
                "SET ARITHABORT ON;"
            )
            cursor.close()
            return SqlConnection(raw, cfg, database)
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                log.debug("connect to %s failed (%s), retrying once", cfg.address, exc)
                time.sleep(1)

    raise ConnectionError_(f"{cfg.address}: {_short_error(last_error)}") from last_error


def _batch(sql: str) -> str:
    """Suppress DONE_IN_PROC row counts.

    Without this, a batch like ``INSERT #t EXEC master..xp_fixeddrives`` (the legacy drive query)
    hands the driver a row-count result set before the real one.
    """
    return "SET NOCOUNT ON;\n" + sql


def _fetch_dicts(cursor: Any) -> list[dict]:
    """Return the first result set that actually has columns.

    A batch that opens with ``USE [db];`` or an ``INSERT ... EXEC`` produces a leading result set
    with no description, so the rows would otherwise look like an empty answer -- which is exactly
    how a per-database collector would silently return nothing on every server.
    """
    while cursor.description is None:
        if not cursor.nextset():
            return []
    columns = [c[0] for c in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _quote_name(name: str) -> str:
    """T-SQL bracket quoting, doubling any embedded closing bracket."""
    return "[" + name.replace("]", "]]") + "]"


def _short_error(exc: Exception | None) -> str:
    """ODBC errors are verbose; keep the driver message, drop the stack of SQLSTATE noise."""
    if exc is None:
        return "unknown error"
    text = str(exc)
    if "[SQL Server]" in text:
        text = text.split("[SQL Server]", 1)[1]
    return text.strip().strip("()'\" ")[:400]


def probe_all(servers: Iterable[_Connectable]) -> dict[str, str | None]:
    """Connectivity check used by ``test-conn``: returns ``{address: error or None}``."""
    results: dict[str, str | None] = {}
    for cfg in servers:
        try:
            with connect(cfg):
                results[cfg.address] = None
        except Exception as exc:
            results[cfg.address] = str(exc)
    return results
