"""Configuration validation and threshold override precedence."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sqlhealthwatch.config import (
    AlertsConfig,
    ConfigError,
    ServerConfig,
    ServerInventory,
    Thresholds,
    _apply_server_defaults,
    load_config,
)


class TestServerConfig:
    def test_sql_auth_requires_a_secret_reference(self):
        # A password inlined in YAML is exactly what this rejects.
        with pytest.raises(ValidationError, match="password_ref"):
            ServerConfig(name="S1", host="h", auth="sql", username="svc")

    def test_sql_auth_requires_a_username(self):
        with pytest.raises(ValidationError, match="username"):
            ServerConfig(name="S1", host="h", auth="sql", password_ref="env:PW")

    def test_windows_auth_needs_no_credentials(self):
        assert ServerConfig(name="S1", host="h", auth="windows").password_ref is None

    def test_address_uses_host_and_port(self):
        assert ServerConfig(name="S1", host="h", port=1433).address == "h,1433"

    def test_address_uses_the_named_instance_when_given(self):
        assert ServerConfig(name="S1", host="h", instance="SQL2019").address == "h\\SQL2019"

    def test_duplicate_names_are_rejected(self):
        with pytest.raises(ValidationError, match="duplicate server names"):
            ServerInventory(
                servers=[ServerConfig(name="S1", host="a"), ServerConfig(name="S1", host="b")]
            )


class TestServerDefaults:
    def test_defaults_are_folded_into_each_server(self):
        raw = {
            "defaults": {"query_timeout_s": 45, "tags": ["fleet"]},
            "servers": [{"name": "S1", "host": "h", "tags": ["tier1"]}],
        }
        inventory = ServerInventory.model_validate(_apply_server_defaults(raw))
        server = inventory.servers[0]

        assert server.query_timeout_s == 45
        # Tags are additive rather than replaced, so a fleet-wide tag survives a per-server list.
        assert server.tags == ["fleet", "tier1"]

    def test_a_server_can_override_a_default(self):
        raw = {"defaults": {"query_timeout_s": 45}, "servers": [{"name": "S1", "host": "h",
                                                                 "query_timeout_s": 120}]}
        inventory = ServerInventory.model_validate(_apply_server_defaults(raw))
        assert inventory.servers[0].query_timeout_s == 120


class TestThresholdPrecedence:
    @pytest.fixture
    def thresholds(self) -> Thresholds:
        return Thresholds(
            defaults={
                "cpu": {"sustained_pct_warn": 80, "sustained_pct_crit": 90},
                "io": {"read_latency_ms_warn": 20, "write_latency_ms_warn": 20},
            },
            overrides={
                "by_tag": {"reporting": {"io": {"read_latency_ms_warn": 40}}},
                "by_server": {"PRD-SQL-01": {"cpu": {"sustained_pct_warn": 85}}},
            },
        )

    def test_defaults_apply_with_no_overrides(self, thresholds):
        plain = ServerConfig(name="OTHER", host="h")
        effective = thresholds.effective(plain)
        assert effective["cpu"]["sustained_pct_warn"] == 80
        assert effective["io"]["read_latency_ms_warn"] == 20

    def test_tag_override_wins_over_the_default(self, thresholds):
        reporting = ServerConfig(name="DW-01", host="h", tags=["reporting"])
        assert thresholds.effective(reporting)["io"]["read_latency_ms_warn"] == 40

    def test_tag_override_merges_rather_than_replacing_the_category(self, thresholds):
        reporting = ServerConfig(name="DW-01", host="h", tags=["reporting"])
        effective = thresholds.effective(reporting)
        # Only the read threshold was overridden; the write threshold must survive.
        assert effective["io"]["write_latency_ms_warn"] == 20

    def test_server_override_wins_over_tag_and_default(self, thresholds):
        server = ServerConfig(name="PRD-SQL-01", host="h", tags=["reporting"])
        effective = thresholds.effective(server)
        assert effective["cpu"]["sustained_pct_warn"] == 85
        assert effective["io"]["read_latency_ms_warn"] == 40  # tag still applies elsewhere

    def test_effective_does_not_mutate_the_defaults(self, thresholds):
        server = ServerConfig(name="PRD-SQL-01", host="h")
        thresholds.effective(server)
        assert thresholds.defaults["cpu"]["sustained_pct_warn"] == 80


class TestAlertRouting:
    def test_default_routing_by_severity(self):
        alerts = AlertsConfig(routing={"crit": ["email", "teams"], "warn": ["email"]})
        server = ServerConfig(name="S1", host="h")
        assert alerts.routing_for(server, "crit") == ["email", "teams"]
        assert alerts.routing_for(server, "warn") == ["email"]

    def test_tag_routing_overrides_the_fleet_default(self):
        alerts = AlertsConfig.model_validate(
            {
                "routing": {"crit": ["email"]},
                "by_tag": {"tier1": {"routing": {"crit": ["email", "teams", "slack"]}}},
            }
        )
        server = ServerConfig(name="S1", host="h", tags=["tier1"])
        assert alerts.routing_for(server, "crit") == ["email", "teams", "slack"]

    def test_unrouted_severity_is_empty(self):
        alerts = AlertsConfig(routing={"crit": ["email"]})
        assert alerts.routing_for(ServerConfig(name="S1", host="h"), "info") == []


class TestLoader:
    def test_the_shipped_config_loads(self, project_root):
        config = load_config(project_root / "config", project_root)
        assert config.inventory.servers
        assert config.settings.repository.schema_name == "mon"
        assert config.settings.retention.raw_days == 7

    def test_missing_file_is_reported_clearly(self, tmp_path):
        with pytest.raises(ConfigError, match="missing config file"):
            load_config(tmp_path)

    def test_invalid_yaml_is_rejected(self, tmp_path, project_root):
        for name in ("servers.yml", "thresholds.yml", "alerts.yml"):
            (tmp_path / name).write_text((project_root / "config" / name).read_text(encoding="utf-8"),
                                         encoding="utf-8")
        (tmp_path / "settings.yml").write_text("repository: [this is not a mapping", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(tmp_path, project_root)

    def test_unknown_key_is_rejected_rather_than_silently_ignored(self, tmp_path, project_root):
        for name in ("settings.yml", "thresholds.yml", "alerts.yml"):
            (tmp_path / name).write_text((project_root / "config" / name).read_text(encoding="utf-8"),
                                         encoding="utf-8")
        (tmp_path / "servers.yml").write_text(
            "servers:\n  - name: S1\n    host: h\n    typoed_key: true\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError):
            load_config(tmp_path, project_root)


class TestPackaging:
    """pyproject.toml is authoritative; requirements.txt mirrors it for pip and the IDE.

    Two declarations of the same thing drift the moment someone adds a dependency to one of them,
    and the symptom is a collector host that installs cleanly and then fails on an import.
    """

    def _requirement_names(self, path):
        import re

        names = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#")[0].strip()
            if line and not line.startswith("-"):
                names.add(re.split(r"[><=!]", line)[0].strip().lower())
        return names

    def _pyproject(self, project_root):
        import tomllib

        return tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))

    def test_requirements_matches_pyproject(self, project_root):
        import re

        declared = {
            re.split(r"[><=!]", dep)[0].lower()
            for dep in self._pyproject(project_root)["project"]["dependencies"]
        }
        listed = self._requirement_names(project_root / "requirements.txt")
        assert listed == declared

    def test_dev_requirements_cover_the_dev_extra(self, project_root):
        import re

        extra = {
            re.split(r"[><=!]", dep)[0].lower()
            for dep in self._pyproject(project_root)["project"]["optional-dependencies"]["dev"]
        }
        listed = self._requirement_names(project_root / "requirements-dev.txt")
        assert extra <= listed

    def test_build_requirements_cover_the_build_extra(self, project_root):
        import re

        extra = {
            re.split(r"[><=!]", dep)[0].lower()
            for dep in self._pyproject(project_root)["project"]["optional-dependencies"]["build"]
        }
        listed = self._requirement_names(project_root / "requirements-build.txt")
        assert extra <= listed

    def test_the_build_toolchain_is_not_a_runtime_dependency(self, project_root):
        # PyInstaller must never end up on a collector host as a runtime requirement.
        runtime = self._requirement_names(project_root / "requirements.txt")
        assert "pyinstaller" not in runtime

    def test_dev_requirements_include_the_runtime_set(self, project_root):
        # The unit suite imports the collector modules, so it needs the runtime deps too.
        text = (project_root / "requirements-dev.txt").read_text(encoding="utf-8")
        assert "-r requirements.txt" in text

    def test_the_entry_point_is_the_main_module(self, project_root):
        scripts = self._pyproject(project_root)["project"]["scripts"]
        assert scripts["sqlhealthwatch"] == "sqlhealthwatch.__main__:main"
