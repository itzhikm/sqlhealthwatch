"""Logging setup: rotating file handler plus console, with an optional JSON format.

Every record carries the ``run_id`` of the tier run that produced it, so a day's log can be sliced
back to a single collection pass.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from pathlib import Path

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class RunIdFilter(logging.Filter):
    """Stamps the current run onto every record; set once by the runner."""

    def __init__(self) -> None:
        super().__init__()
        self.run_id = "-"

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "run_id"):
            record.run_id = self.run_id
        return True


run_id_filter = RunIdFilter()


def setup_logging(
    log_dir: Path,
    level: str = "INFO",
    json_format: bool = False,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 7,
) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)

    if json_format:
        formatter: logging.Formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)-5s [%(run_id)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
        )

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "sqlhealthwatch.log", maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(run_id_filter)
    root.addHandler(file_handler)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    console.addFilter(run_id_filter)
    root.addHandler(console)


def set_run_id(run_id: str) -> None:
    run_id_filter.run_id = run_id
