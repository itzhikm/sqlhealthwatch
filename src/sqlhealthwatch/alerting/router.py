"""Alert routing: what actually gets pushed, and to whom.

Every threshold breach becomes a finding, but findings are not alerts. The router decides which ones
leave the building:

    severity    only warn and crit -- info findings are recorded in mon.findings and left there
    category    fast-tier categories only (cpu, memory, io, space, blocking, availability, deadlock);
                index, stats and query findings are recorded, not paged
    cooldown    the same fingerprint is not re-sent within the cooldown window, so a disk that has
                been 90% full since Tuesday does not page every 15 minutes
    quiet hours suppressed overnight, except crit when allow_crit is set
    routing     per-severity channels, with per-tag overrides (tier1 can add Teams and Slack)

A channel failure is recorded and does not stop the other channels, and never fails the run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..analyze.thresholds import Finding
from ..config import AlertsConfig, ServerConfig
from ..util.timeutil import in_quiet_hours

log = logging.getLogger(__name__)

ALERTABLE_SEVERITIES = {"warn", "crit"}
# Index/stats/query findings are informational by design: they are recorded in mon.findings for a
# DBA to query, not put on a pager at 03:00.
ALERTABLE_CATEGORIES = {"cpu", "memory", "io", "space", "blocking", "deadlock", "availability"}


@dataclass
class AlertDecision:
    finding: Finding
    channels: list[str]
    suppressed_reason: str | None = None

    @property
    def sent(self) -> bool:
        return self.suppressed_reason is None and bool(self.channels)


@dataclass
class RoutingResult:
    decisions: list[AlertDecision] = field(default_factory=list)
    delivered: int = 0
    failures: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        suppressed = sum(1 for d in self.decisions if not d.sent)
        return f"{self.delivered} alert(s) delivered, {suppressed} suppressed"


class AlertRouter:
    def __init__(self, config: AlertsConfig, repository=None, now: datetime | None = None) -> None:
        self.config = config
        self.repository = repository
        self.now = now or datetime.now()
        self._channels = _build_channels(config)

    # ------------------------------------------------------------------------------ decisions

    def decide(self, finding: Finding, server: ServerConfig) -> AlertDecision:
        if finding.severity not in ALERTABLE_SEVERITIES:
            return AlertDecision(finding, [], "informational -- recorded, not alerted")
        if finding.category not in ALERTABLE_CATEGORIES:
            return AlertDecision(finding, [], f"{finding.category} findings are recorded, not paged")

        channels = self.config.routing_for(server, finding.severity)
        if not channels:
            return AlertDecision(finding, [], f"no channel routed for severity {finding.severity}")

        enabled = [name for name in channels if name in self._channels]
        if not enabled:
            return AlertDecision(finding, [], f"routed channels are disabled: {', '.join(channels)}")

        if self._in_cooldown(finding):
            return AlertDecision(finding, [], f"within the {self.config.cooldown_minutes}-minute cooldown")

        if self._quiet(finding):
            return AlertDecision(finding, [], "quiet hours")

        return AlertDecision(finding, enabled)

    def _in_cooldown(self, finding: Finding) -> bool:
        if self.repository is None or self.config.cooldown_minutes <= 0:
            return False
        try:
            last = self.repository.last_alert_time(finding.fingerprint)
        except Exception as exc:  # pragma: no cover - a repo hiccup must not silence alerts
            log.warning("cooldown lookup failed for %s: %s", finding.fingerprint, exc)
            return False
        if last is None:
            return False
        return (datetime.utcnow() - last) < timedelta(minutes=self.config.cooldown_minutes)

    def _quiet(self, finding: Finding) -> bool:
        quiet = self.config.quiet_hours
        if not in_quiet_hours(self.now, quiet.start, quiet.end):
            return False
        # A critical finding is exactly what quiet hours should still wake someone for.
        return not (quiet.allow_crit and finding.severity == "crit")

    # -------------------------------------------------------------------------------- sending

    def dispatch(self, findings: list[Finding], server: ServerConfig, server_id: int | None = None,
                 dry_run: bool = False) -> RoutingResult:
        result = RoutingResult()
        for finding in findings:
            decision = self.decide(finding, server)
            result.decisions.append(decision)
            if not decision.sent or dry_run:
                if decision.suppressed_reason:
                    log.debug("suppressed %s: %s", finding.fingerprint, decision.suppressed_reason)
                continue

            for channel_name in decision.channels:
                channel = self._channels[channel_name]
                try:
                    channel.send(finding, server, self.config)
                    result.delivered += 1
                    self._record(server_id, finding, channel_name, True, None)
                except Exception as exc:
                    log.warning("alert via %s failed: %s", channel_name, exc)
                    result.failures[channel_name] = str(exc)
                    self._record(server_id, finding, channel_name, False, str(exc))
        return result

    def _record(self, server_id: int | None, finding: Finding, channel: str, ok: bool,
                error: str | None) -> None:
        if self.repository is None or server_id is None:
            return
        try:
            self.repository.log_alert(server_id, finding.fingerprint, finding.severity, channel, ok, error)
        except Exception as exc:  # pragma: no cover
            log.warning("could not record the alert in alert_log: %s", exc)


def _build_channels(config: AlertsConfig) -> dict:
    from . import email, slack, teams

    channels = {}
    if config.channels.email.enabled:
        channels["email"] = email.EmailChannel(config.channels.email)
    if config.channels.teams.enabled:
        channels["teams"] = teams.TeamsChannel(config.channels.teams)
    if config.channels.slack.enabled:
        channels["slack"] = slack.SlackChannel(config.channels.slack)
    return channels


def format_subject(finding: Finding) -> str:
    return f"[{finding.severity.upper()}] {finding.server_name}: {finding.message[:120]}"


def format_body(finding: Finding, config: AlertsConfig) -> str:
    """Plain-text alert body: the finding, and the numbers that triggered it."""
    lines = [
        finding.message,
        "",
        f"Server:    {finding.server_name}",
        f"Category:  {finding.category}",
        f"Metric:    {finding.metric}",
    ]
    if finding.observed is not None:
        lines.append(f"Observed:  {finding.observed:g}")
    if finding.threshold is not None:
        lines.append(f"Threshold: {finding.threshold:g}")
    if finding.created_utc:
        lines.append(f"Time:      {finding.created_utc:%Y-%m-%d %H:%M} UTC")

    return "\n".join(lines)
