"""Slack incoming-webhook channel. Optional in v1, same shape as the Teams channel."""

from __future__ import annotations

import json
import logging
import urllib.request

from ..config import AlertsConfig, ServerConfig, WebhookChannel as WebhookConfig
from ..util import secrets

log = logging.getLogger(__name__)

POST_TIMEOUT_S = 10
SEVERITY_EMOJI = {"crit": ":rotating_light:", "warn": ":warning:", "info": ":information_source:"}


class SlackChannel:
    name = "slack"

    def __init__(self, config: WebhookConfig) -> None:
        self.config = config

    def send(self, finding, server: ServerConfig, alerts: AlertsConfig) -> None:
        url = secrets.resolve(self.config.webhook_ref)
        if not url:
            raise ValueError("slack channel is enabled but webhook_ref resolves to nothing")

        emoji = SEVERITY_EMOJI.get(finding.severity, "")
        fields = [
            {"type": "mrkdwn", "text": f"*Category*\n{finding.category}"},
            {"type": "mrkdwn", "text": f"*Metric*\n{finding.metric}"},
        ]
        if finding.observed is not None:
            fields.append({"type": "mrkdwn", "text": f"*Observed*\n{finding.observed:g}"})
        if finding.threshold is not None:
            fields.append({"type": "mrkdwn", "text": f"*Threshold*\n{finding.threshold:g}"})

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"{emoji} *{finding.severity.upper()} — {finding.server_name}*\n{finding.message}",
                },
            },
            {"type": "section", "fields": fields},
        ]


        _post(url, {"text": f"{finding.severity.upper()} {finding.server_name}: {finding.message}",
                    "blocks": blocks})
        log.info("posted %s to Slack", finding.fingerprint)


def _post(url: str, payload: dict) -> None:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=POST_TIMEOUT_S) as response:
        if response.status >= 300:
            raise RuntimeError(f"webhook returned HTTP {response.status}")
