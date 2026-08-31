"""Microsoft Teams incoming-webhook channel.

Optional in v1. Uses ``urllib`` rather than adding an HTTP dependency for two POSTs, and the webhook
URL -- which is itself a credential -- comes from a secret reference.
"""

from __future__ import annotations

import json
import logging
import urllib.request

from ..config import AlertsConfig, ServerConfig, WebhookChannel as WebhookConfig
from ..util import secrets

log = logging.getLogger(__name__)

POST_TIMEOUT_S = 10
SEVERITY_COLOR = {"crit": "D93025", "warn": "F9AB00", "info": "1A73E8"}


class TeamsChannel:
    name = "teams"

    def __init__(self, config: WebhookConfig) -> None:
        self.config = config

    def send(self, finding, server: ServerConfig, alerts: AlertsConfig) -> None:
        url = secrets.resolve(self.config.webhook_ref)
        if not url:
            raise ValueError("teams channel is enabled but webhook_ref resolves to nothing")

        facts = [
            {"name": "Server", "value": finding.server_name},
            {"name": "Category", "value": finding.category},
            {"name": "Metric", "value": finding.metric},
        ]
        if finding.observed is not None:
            facts.append({"name": "Observed", "value": f"{finding.observed:g}"})
        if finding.threshold is not None:
            facts.append({"name": "Threshold", "value": f"{finding.threshold:g}"})

        card = {
            "@type": "MessageCard",
            "@context": "https://schema.org/extensions",
            "themeColor": SEVERITY_COLOR.get(finding.severity, "1A73E8"),
            "summary": f"{finding.severity.upper()} {finding.server_name}",
            "title": f"{finding.severity.upper()}: {finding.server_name}",
            "text": finding.message,
            "sections": [{"facts": facts}],
        }

        _post(url, card)
        log.info("posted %s to Teams", finding.fingerprint)


def _post(url: str, payload: dict) -> None:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=POST_TIMEOUT_S) as response:
        if response.status >= 300:
            raise RuntimeError(f"webhook returned HTTP {response.status}")
