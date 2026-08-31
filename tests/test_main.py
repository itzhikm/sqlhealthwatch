"""The entry point: argument parsing, command dispatch and exit codes.

Exit codes matter more than usual here, because a Windows Task Scheduler job's "last result" is the
only place an operator sees whether a collection run worked.
"""

from __future__ import annotations

import pytest

from sqlhealthwatch.__main__ import EXIT_CONFIG, EXIT_FAILED, EXIT_OK, build_parser, main


class TestArguments:
    def test_no_command_runs_the_fast_tier(self):
        # So hitting Run on the module in an IDE does the useful thing.
        assert build_parser().parse_args([]).command == "fast"

    def test_tiers_are_selectable(self):
        assert build_parser().parse_args(["daily"]).command == "daily"

    def test_config_directory_defaults_to_config(self):
        assert build_parser().parse_args([]).config == "config"

    def test_flags_are_parsed(self):
        args = build_parser().parse_args(["fast", "--server", "PRD-SQL-01", "--dry-run",
                                          "-c", "other"])
        assert args.server == "PRD-SQL-01"
        assert args.dry_run is True
        assert args.config == "other"

    def test_an_unknown_command_is_rejected(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["render-report"])


class TestCollectorsCommand:
    def test_it_lists_every_collector_without_touching_config(self, capsys):
        # Deliberately runs before configuration is loaded, so it works on a fresh checkout.
        assert main(["collectors"]) == EXIT_OK

        out = capsys.readouterr().out
        assert "fast   cpu" in out
        assert "daily  index_frag" in out

    def test_every_collector_names_a_table(self, capsys):
        main(["collectors"])
        for line in capsys.readouterr().out.splitlines():
            assert "-> mon." in line
            assert not line.rstrip().endswith("mon."), f"collector with no table: {line}"


class TestConfigErrors:
    def test_a_missing_config_directory_exits_two(self, tmp_path, capsys):
        assert main(["fast", "-c", str(tmp_path / "absent")]) == EXIT_CONFIG
        assert "Configuration error" in capsys.readouterr().err


class TestTierDispatch:
    @pytest.fixture
    def stub_run(self, monkeypatch, project_root):
        """Point the entry point at the shipped config, with the runner stubbed out."""
        import sqlhealthwatch.__main__ as entry

        calls = {}

        class FakeResult:
            def __init__(self, ok=1, failed=0, skipped=False):
                self.results = []
                self.ok_count = ok
                self.failed_count = failed
                self.skipped = skipped
                self.skip_reason = "already running" if skipped else None

            def summary(self):
                return "fast run: stubbed"

        def apply(result):
            def run_tier(config, tier, only=None, dry_run=False):
                calls.update(tier=tier, only=only, dry_run=dry_run)
                return result

            monkeypatch.setattr("sqlhealthwatch.runner.run_tier", run_tier)
            monkeypatch.setattr(entry, "setup_logging", lambda *a, **k: None)
            return calls

        apply.result = FakeResult
        return apply

    def _argv(self, project_root, *rest):
        return [*rest, "-c", str(project_root / "config")]

    def test_fast_tier_is_dispatched(self, stub_run, project_root):
        calls = stub_run(stub_run.result())
        assert main(self._argv(project_root, "fast")) == EXIT_OK
        assert calls["tier"] == "fast" and calls["only"] is None

    def test_daily_tier_is_dispatched(self, stub_run, project_root):
        calls = stub_run(stub_run.result())
        assert main(self._argv(project_root, "daily")) == EXIT_OK
        assert calls["tier"] == "daily"

    def test_server_filter_and_dry_run_reach_the_runner(self, stub_run, project_root):
        calls = stub_run(stub_run.result())
        main(self._argv(project_root, "fast", "--server", "PRD-SQL-01", "--dry-run"))
        assert calls["only"] == ["PRD-SQL-01"] and calls["dry_run"] is True

    def test_a_partly_failed_run_still_succeeds(self, stub_run, project_root):
        # One unreachable server out of forty is not a failed run.
        stub_run(stub_run.result(ok=39, failed=1))
        assert main(self._argv(project_root, "fast")) == EXIT_OK

    def test_a_run_where_nothing_succeeded_fails(self, stub_run, project_root):
        stub_run(stub_run.result(ok=0, failed=40))
        assert main(self._argv(project_root, "fast")) == EXIT_FAILED

    def test_a_skipped_run_is_not_a_failure(self, stub_run, project_root, capsys):
        # The overlap guard skipping a run is normal operation, not an error to alert on.
        stub_run(stub_run.result(skipped=True))
        assert main(self._argv(project_root, "fast")) == EXIT_OK

    def test_an_exception_in_the_runner_exits_one(self, monkeypatch, project_root, capsys):
        import sqlhealthwatch.__main__ as entry

        def explode(*args, **kwargs):
            raise RuntimeError("repository unreachable")

        monkeypatch.setattr("sqlhealthwatch.runner.run_tier", explode)
        monkeypatch.setattr(entry, "setup_logging", lambda *a, **k: None)

        assert main(self._argv(project_root, "fast")) == EXIT_FAILED
        assert "repository unreachable" in capsys.readouterr().err


class TestUsageLine:
    def test_module_form_by_default(self):
        # Running from source, the usage line has to be the module invocation.
        assert build_parser().prog == "python -m sqlhealthwatch"

    def test_exe_name_when_frozen(self, monkeypatch):
        # PyInstaller sets sys.frozen; the usage line must then name the exe, not `python -m`.
        monkeypatch.setattr("sys.frozen", True, raising=False)
        assert build_parser().prog == "sqlhealthwatch"


class TestDirectExecution:
    """An IDE's Run button executes the file as a script, with no parent package.

    Without the package-context guard at the top of __main__.py, every relative import in it fails
    with "attempted relative import with no known parent package" -- so the documented "just hit
    Run" has to be verified, not assumed.
    """

    def _run(self, project_root, *args):
        import subprocess
        import sys

        return subprocess.run(
            [sys.executable, str(project_root / "src" / "sqlhealthwatch" / "__main__.py"), *args],
            capture_output=True, text=True, cwd=str(project_root.parent),
        )

    def test_running_the_file_as_a_script_works(self, project_root):
        result = self._run(project_root, "collectors")
        assert result.returncode == 0, result.stderr
        assert "cpu" in result.stdout

    def test_no_relative_import_error(self, project_root):
        result = self._run(project_root, "collectors")
        assert "attempted relative import" not in result.stderr

    def test_the_module_form_still_works(self, project_root):
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-m", "sqlhealthwatch", "collectors"],
            capture_output=True, text=True, cwd=str(project_root),
        )
        assert result.returncode == 0, result.stderr
        assert "cpu" in result.stdout
