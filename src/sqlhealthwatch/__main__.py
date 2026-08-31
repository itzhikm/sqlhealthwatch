"""Entry point for the collector.

Run it as a module:

    python -m sqlhealthwatch                  # fast tier (the default)
    python -m sqlhealthwatch fast
    python -m sqlhealthwatch daily
    python -m sqlhealthwatch test-conn --all
    python -m sqlhealthwatch prune
    python -m sqlhealthwatch collectors

With no arguments it runs the fast tier, so an IDE's Run button on this file does the useful thing.
In production this is driven by two Windows Task Scheduler jobs rather than a long-lived process --
they survive reboots, and last-run status shows up in a tool the operations team already uses.

The collector's only output is the ``DBA_Monitoring`` repository (plus threshold alerts and the
optional Parquet archive). There is no report and no web front end: query the ``mon`` tables.
"""

from __future__ import annotations

import argparse
import logging
import sys

# An IDE's Run button executes this file as a top-level script rather than as `python -m
# sqlhealthwatch`. Python then gives it no parent package, and every `from .config import ...` below
# fails with "attempted relative import with no known parent package". Re-establish the package
# context so the file works run either way -- otherwise the documented "just hit Run" is a lie.
if __package__ in (None, ""):  # pragma: no cover - only taken when run as a script
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "sqlhealthwatch"

try:
    from .config import AppConfig, ConfigError, load_config
    from .util.logging import setup_logging
except ModuleNotFoundError as exc:  # pragma: no cover - depends on the running interpreter
    # Overwhelmingly this means the wrong interpreter: an IDE run configuration still pointing at
    # another project's virtualenv, or a collector host where the dependencies were installed into
    # a different Python. A bare "No module named 'yaml'" does not say that, so name the culprit.
    raise SystemExit(
        f"sqlhealthwatch: missing dependency {exc.name!r}.\n"
        f"  running interpreter : {sys.executable}\n"
        f"  This is usually the wrong interpreter rather than a broken install.\n"
        f"  Fix: point your IDE (or PATH) at the project's own virtualenv, or install into\n"
        f"       the interpreter above with:  pip install -e \".[dev]\""
    ) from exc

log = logging.getLogger("sqlhealthwatch")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2


def build_parser() -> argparse.ArgumentParser:
    # Frozen by PyInstaller (or installed as the console script) the invocation is the exe name,
    # not the module form -- so the usage line has to follow how it was actually launched.
    frozen = getattr(sys, "frozen", False)
    parser = argparse.ArgumentParser(
        prog="sqlhealthwatch" if frozen else "python -m sqlhealthwatch",
        description="Health collector for a fleet of SQL Server instances.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "With no command, the fast tier runs.\n"
            "Collected data lands in the DBA_Monitoring repository; query the mon schema."
        ),
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="fast",
        choices=["fast", "daily", "test-conn", "prune", "collectors"],
        help="what to run (default: fast)",
    )
    parser.add_argument("-c", "--config", default="config", metavar="DIR",
                        help="directory holding the YAML config files (default: config)")
    parser.add_argument("-s", "--server", metavar="NAME",
                        help="limit the run, or the connectivity check, to one server")
    parser.add_argument("--dry-run", action="store_true",
                        help="collect and evaluate thresholds, but write nothing and send no alerts")
    parser.add_argument("--repo", action="store_true",
                        help="test-conn: check repository write access and schema instead of the fleet")
    parser.add_argument("--maintain-indexes", action="store_true",
                        help="prune: also reorganize and update statistics on the repository itself")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "collectors":
        return _list_collectors()

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    setup_logging(
        config.resolve_path(config.settings.paths.logs),
        level=config.settings.logging.level,
        json_format=config.settings.logging.json_format,
        max_bytes=config.settings.logging.max_bytes,
        backup_count=config.settings.logging.backup_count,
    )

    if args.command in ("fast", "daily"):
        return _run_tier(config, args)
    if args.command == "test-conn":
        return _test_conn(config, args)
    if args.command == "prune":
        return _prune(config, args)
    return EXIT_OK  # unreachable: argparse restricts the choices


# ------------------------------------------------------------------------------------ commands


def _run_tier(config: AppConfig, args) -> int:
    from .runner import run_tier

    try:
        result = run_tier(config, args.command, only=[args.server] if args.server else None,
                          dry_run=args.dry_run)
    except Exception as exc:
        log.exception("%s run failed", args.command)
        print(f"{args.command} run failed: {exc}", file=sys.stderr)
        return EXIT_FAILED

    print(result.summary())
    if result.skipped:
        return EXIT_OK

    for entry in sorted(result.results, key=lambda r: r.server.name):
        if entry.ok:
            print(f"  OK   {entry.server.name:<20} {entry.total_rows:>6} rows  {entry.duration_ms:>6} ms")
            for note in entry.notes:
                print(f"       note: {note}")
        else:
            print(f"  FAIL {entry.server.name:<20} {entry.error}")

    # A run where every server failed is a failed run; one bad server out of forty is not.
    return EXIT_FAILED if result.ok_count == 0 else EXIT_OK


def _test_conn(config: AppConfig, args) -> int:
    if args.repo:
        return _check_repository(config)

    from .runner import test_connections

    failures = 0
    for entry in test_connections(config, [args.server] if args.server else None):
        if entry["ok"]:
            print(f"  OK   {entry['server']:<20} {entry['address']}")
            print(f"       {entry['version']} - {entry['edition']}")
            print(f"       {entry['databases']} database(s), "
                  f"{entry['query_store_databases']} with Query Store")
            for limitation in entry["limitations"]:
                print(f"       limited: {limitation}")
        else:
            failures += 1
            print(f"  FAIL {entry['server']:<20} {entry['error']}")

    return EXIT_FAILED if failures else EXIT_OK


def _check_repository(config: AppConfig) -> int:
    from .storage.repository import Repository

    settings = config.settings
    try:
        with Repository(settings.repository, settings) as repo:
            if not repo.schema_exists():
                if not settings.repository.auto_bootstrap:
                    print("  FAIL repository schema is missing and auto_bootstrap is off. "
                          "Run sql/repository/create_database.sql.", file=sys.stderr)
                    return EXIT_FAILED
                print("  schema not found -- bootstrapping")
                repo.bootstrap()

            # Prove write access, not just connectivity: a read-only login passes a SELECT.
            probe = repo.ensure_server("__connectivity_probe__")
            repo.record_server_status("00000000-0000-0000-0000-000000000000", probe, True, 0,
                                      "test-conn write probe")
            size = repo.repository_size_mb()
            suffix = f" ({size:.0f} MB)" if size else ""
            print(f"  OK   repository {settings.repository.database} on "
                  f"{settings.repository.address}{suffix}")
        return EXIT_OK
    except Exception as exc:
        print(f"  FAIL repository: {exc}", file=sys.stderr)
        return EXIT_FAILED


def _prune(config: AppConfig, args) -> int:
    from .storage import retention
    from .storage.repository import Repository

    with Repository(config.settings.repository, config.settings) as repo:
        result = retention.prune(repo, config.settings.retention)
        print(f"  {result.summary()}")
        for table, error in result.errors.items():
            print(f"  {table}: {error}", file=sys.stderr)

        if args.maintain_indexes or config.settings.retention.rebuild_repo_indexes == "weekly":
            tables = retention.rebuild_repository_indexes(repo)
            print(f"  index maintenance on {len(tables)} table(s)")

    removed = retention.prune_exports(
        config.resolve_path(config.settings.paths.exports),
        config.settings.retention.export_retention_days,
    )
    if removed:
        print(f"  removed {removed} expired export folder(s)")
    return EXIT_OK


def _list_collectors() -> int:
    """What runs in which tier, writing which table. Needs no configuration."""
    from .collectors import ALL_COLLECTORS

    for collector in ALL_COLLECTORS:
        print(f"  {collector.tier:<6} {collector.name:<18} -> mon.{collector.table}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
