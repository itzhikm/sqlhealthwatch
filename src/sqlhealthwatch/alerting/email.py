"""SMTP alert channel -- the safe default for v1.

Corporate relays commonly accept unauthenticated mail from a known host on port 25, so username and
TLS are both optional; the password, when there is one, comes from a secret reference like every
other credential.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from ..config import AlertsConfig, EmailChannel as EmailChannelConfig, ServerConfig
from ..util import secrets
from .router import format_body, format_subject

log = logging.getLogger(__name__)

SMTP_TIMEOUT_S = 15


class EmailChannel:
    name = "email"

    def __init__(self, config: EmailChannelConfig) -> None:
        self.config = config

    def send(self, finding, server: ServerConfig, alerts: AlertsConfig) -> None:
        if not self.config.to:
            raise ValueError("email channel is enabled but has no recipients")

        message = EmailMessage()
        message["Subject"] = format_subject(finding)
        message["From"] = self.config.from_
        message["To"] = ", ".join(self.config.to)
        message.set_content(format_body(finding, alerts))

        with smtplib.SMTP(self.config.smtp_host, self.config.smtp_port, timeout=SMTP_TIMEOUT_S) as smtp:
            if self.config.use_tls:
                smtp.starttls()
            if self.config.username:
                smtp.login(self.config.username, secrets.resolve(self.config.password_ref) or "")
            smtp.send_message(message)
        log.info("emailed %s to %d recipient(s)", finding.fingerprint, len(self.config.to))
