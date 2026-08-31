"""Configuration models and loader.

Four YAML files, validated by pydantic on load so a bad edit fails at startup rather than halfway
through a collection run:

    servers.yml     the fleet inventory
    thresholds.yml  fleet defaults plus per-tag and per-server overrides
    settings.yml    repository, paths, retention, concurrency, tiers
    alerts.yml      channels, routing, cooldown, quiet hours

Secrets are referenced (``password_ref: env:NAME``), never inlined; see util.secrets.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_DRIVER = "ODBC Driver 18 for SQL Server"
AuthMode = Literal["windows", "sql"]
Severity = Literal["info", "warn", "crit"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------------------- servers


class ServerDefaults(_Model):
    driver: str = DEFAULT_DRIVER
    encrypt: bool = True
    trust_server_certificate: bool = True
    connect_timeout_s: int = 5
    query_timeout_s: int = 30
    tags: list[str] = Field(default_factory=list)


class ServerConfig(_Model):
    """One monitored instance. ``name`` is the stable identity used everywhere downstream."""

    name: str
    host: str
    port: int | None = 1433
    instance: str | None = None
    auth: AuthMode = "windows"
    username: str | None = None
    password_ref: str | None = None
    enabled: bool = True
    tags: list[str] = Field(default_factory=list)
    driver: str = DEFAULT_DRIVER
    encrypt: bool = True
    trust_server_certificate: bool = True
    connect_timeout_s: int = 5
    query_timeout_s: int = 30

    @model_validator(mode="after")
    def _check_auth(self) -> "ServerConfig":
        if self.auth == "sql":
            if not self.username:
                raise ValueError(f"server {self.name}: auth 'sql' requires a username")
            if not self.password_ref:
                raise ValueError(
                    f"server {self.name}: auth 'sql' requires password_ref "
                    f"(env:/credman:/dpapi:) -- passwords are never inlined in YAML"
                )
        if self.instance and self.port not in (None, 1433):
            # Both is not an error, but the port wins and the instance name is then cosmetic.
            pass
        return self

    @property
    def address(self) -> str:
        """SERVER= value: host\\instance for named instances, host,port otherwise."""
        if self.instance:
            return f"{self.host}\\{self.instance}"
        return f"{self.host},{self.port or 1433}"


class ServerInventory(_Model):
    defaults: ServerDefaults = Field(default_factory=ServerDefaults)
    servers: list[ServerConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_names(self) -> "ServerInventory":
        names = [s.name for s in self.servers]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(f"duplicate server names in servers.yml: {sorted(duplicates)}")
        return self

    @property
    def enabled(self) -> list[ServerConfig]:
        return [s for s in self.servers if s.enabled]

    def get(self, name: str) -> ServerConfig | None:
        return next((s for s in self.servers if s.name == name), None)


# -------------------------------------------------------------------------------------- settings


class BulkConfig(_Model):
    fast_executemany: bool = True
    batch_rows: int = 5000


class RepositoryConfig(_Model):
    name: str = "REPO"
    host: str
    port: int | None = 1433
    instance: str | None = None
    database: str = "DBA_Monitoring"
    schema_name: str = Field("mon", alias="schema")
    auth: AuthMode = "windows"
    username: str | None = None
    password_ref: str | None = None
    driver: str = DEFAULT_DRIVER
    encrypt: bool = True
    trust_server_certificate: bool = True
    connect_timeout_s: int = 5
    query_timeout_s: int = 60
    auto_bootstrap: bool = True
    bulk: BulkConfig = Field(default_factory=BulkConfig)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @property
    def address(self) -> str:
        if self.instance:
            return f"{self.host}\\{self.instance}"
        return f"{self.host},{self.port or 1433}"


class PathsConfig(_Model):
    exports: Path = Path("data/exports")
    logs: Path = Path("logs")


class RetentionConfig(_Model):
    raw_days: int = 7
    runs_days: int = 30
    deadlock_days: int = 90
    export_retention_days: int = 30
    prune_strategy: Literal["delete", "partition_switch"] = "delete"
    prune_batch_rows: int = 50000
    rebuild_repo_indexes: Literal["weekly", "never"] = "weekly"


class ConcurrencyConfig(_Model):
    max_workers: int = 8
    per_server_timeout_s: int = 120


class TiersConfig(_Model):
    fast_minutes: int = 15
    daily_time: str = "06:00"


class CollectionConfig(_Model):
    parquet_export: bool = True
    exclude_databases: list[str] = Field(default_factory=lambda: ["tempdb", "model"])
    include_system_databases: bool = False
    query_window_hours: int = 24
    top_n_queries: int = 25
    # 'hash' replaces captured statement text with a digest; 'none' stores it as collected.
    statement_text_mode: Literal["none", "hash"] = "none"


class LoggingConfig(_Model):
    level: str = "INFO"
    # Aliased because a bare `json` field would shadow BaseModel.json.
    json_format: bool = Field(False, alias="json")
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 7

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Settings(_Model):
    repository: RepositoryConfig
    paths: PathsConfig = Field(default_factory=PathsConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    concurrency: ConcurrencyConfig = Field(default_factory=ConcurrencyConfig)
    tiers: TiersConfig = Field(default_factory=TiersConfig)
    collection: CollectionConfig = Field(default_factory=CollectionConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


# ------------------------------------------------------------------------------------ thresholds


class Thresholds(_Model):
    """Threshold sets with override precedence: fleet defaults -> tag -> server.

    Overrides are merged per category, so a tag that raises one IO threshold keeps every other
    default rather than replacing the whole ``io`` block.
    """

    defaults: dict[str, dict[str, Any]] = Field(default_factory=dict)
    overrides: dict[str, dict[str, dict[str, dict[str, Any]]]] = Field(default_factory=dict)

    def effective(self, server: ServerConfig) -> dict[str, dict[str, Any]]:
        merged = copy.deepcopy(self.defaults)
        by_tag = self.overrides.get("by_tag", {})
        for tag in server.tags:
            _deep_merge(merged, by_tag.get(tag, {}))
        _deep_merge(merged, self.overrides.get("by_server", {}).get(server.name, {}))
        return merged


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


# ---------------------------------------------------------------------------------------- alerts


class EmailChannel(_Model):
    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 25
    use_tls: bool = False
    username: str | None = None
    password_ref: str | None = None
    from_: str = Field("", alias="from")
    to: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class WebhookChannel(_Model):
    enabled: bool = False
    webhook_ref: str | None = None


class Channels(_Model):
    email: EmailChannel = Field(default_factory=EmailChannel)
    teams: WebhookChannel = Field(default_factory=WebhookChannel)
    slack: WebhookChannel = Field(default_factory=WebhookChannel)


class QuietHours(_Model):
    start: str = "22:00"
    end: str = "06:00"
    allow_crit: bool = True


class TagAlertOverride(_Model):
    routing: dict[str, list[str]] = Field(default_factory=dict)


class AlertsConfig(_Model):
    channels: Channels = Field(default_factory=Channels)
    routing: dict[str, list[str]] = Field(default_factory=dict)
    cooldown_minutes: int = 60
    quiet_hours: QuietHours = Field(default_factory=QuietHours)
    by_tag: dict[str, TagAlertOverride] = Field(default_factory=dict)

    def routing_for(self, server: ServerConfig, severity: str) -> list[str]:
        """Per-tag routing wins over the fleet default for that severity."""
        channels = list(self.routing.get(severity, []))
        for tag in server.tags:
            override = self.by_tag.get(tag)
            if override and severity in override.routing:
                channels = list(override.routing[severity])
        return channels


# ---------------------------------------------------------------------------------------- loader


class AppConfig(_Model):
    settings: Settings
    inventory: ServerInventory
    thresholds: Thresholds
    alerts: AlertsConfig
    config_dir: Path
    project_root: Path

    @property
    def sql_dir(self) -> Path:
        """Directory holding the DMV queries. Kept outside the package so a DBA can tune them."""
        return self.project_root / "sql"

    def resolve_path(self, path: Path) -> Path:
        return path if path.is_absolute() else self.project_root / path


class ConfigError(RuntimeError):
    pass


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"missing config file: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} must contain a YAML mapping at the top level")
    return data


def _apply_server_defaults(raw: dict[str, Any]) -> dict[str, Any]:
    """Fold servers.yml `defaults` into each server block before validation."""
    defaults = raw.get("defaults") or {}
    servers = []
    for entry in raw.get("servers") or []:
        merged = {**defaults, **entry}
        # Tags are additive: a server keeps the fleet-wide tags plus its own.
        merged["tags"] = list(dict.fromkeys([*defaults.get("tags", []), *entry.get("tags", [])]))
        servers.append(merged)
    return {"defaults": defaults, "servers": servers}


def load_config(config_dir: str | Path = "config", project_root: str | Path | None = None) -> AppConfig:
    """Load and validate all four YAML files.

    ``.env`` is loaded first so that ``env:`` secret references resolve on the collector host.
    """
    config_dir = Path(config_dir)
    root = Path(project_root) if project_root else config_dir.parent.resolve()

    env_file = root / ".env"
    if env_file.exists():
        try:
            from dotenv import load_dotenv

            load_dotenv(env_file)
        except ImportError:  # pragma: no cover - dotenv is a hard dependency in practice
            pass

    try:
        settings = Settings.model_validate(_read_yaml(config_dir / "settings.yml"))
        inventory = ServerInventory.model_validate(
            _apply_server_defaults(_read_yaml(config_dir / "servers.yml"))
        )
        thresholds = Thresholds.model_validate(_read_yaml(config_dir / "thresholds.yml"))
        alerts = AlertsConfig.model_validate(_read_yaml(config_dir / "alerts.yml"))
    except ConfigError:
        raise
    except Exception as exc:  # pydantic ValidationError and friends
        raise ConfigError(f"invalid configuration: {exc}") from exc

    return AppConfig(
        settings=settings,
        inventory=inventory,
        thresholds=thresholds,
        alerts=alerts,
        config_dir=config_dir,
        project_root=root,
    )
