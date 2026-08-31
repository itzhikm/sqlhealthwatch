"""Alert routing: severity and category gates, cooldown dedup, quiet hours, per-tag routing."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from sqlhealthwatch.alerting.router import AlertRouter, format_body
from sqlhealthwatch.analyze.thresholds import Finding
from sqlhealthwatch.config import AlertsConfig, ServerConfig

DAYTIME = datetime(2026, 8, 30, 10, 0)
NIGHT = datetime(2026, 8, 30, 23, 30)


def make_finding(severity="crit", category="space", metric="db_free_pct") -> Finding:
    return Finding(
        server_name="PRD-SQL-01", server_id=1, run_id="run", category=category, severity=severity,
        metric=metric, message="ERP/ERP_Data is 5% free", observed=5.0, threshold=8.0,
        details={"object": "ERP/ERP_Data"}, created_utc=DAYTIME,
    )


def make_alerts(**overrides) -> AlertsConfig:
    payload = {
        "channels": {"email": {"enabled": True, "smtp_host": "smtp.test", "from": "a@b.c",
                               "to": ["dba@b.c"]}},
        "routing": {"crit": ["email", "teams"], "warn": ["email"]},
        "cooldown_minutes": 60,
        "quiet_hours": {"start": "22:00", "end": "06:00", "allow_crit": True},
    }
    payload.update(overrides)
    return AlertsConfig.model_validate(payload)


class FakeRepo:
    def __init__(self, last_alert=None):
        self.last_alert = last_alert
        self.logged = []

    def last_alert_time(self, fingerprint):
        return self.last_alert

    def log_alert(self, server_id, fingerprint, severity, channel, ok, error=None):
        self.logged.append((fingerprint, channel, ok))


@pytest.fixture
def server():
    return ServerConfig(name="PRD-SQL-01", host="h", tags=["tier1"])


class TestSeverityAndCategoryGates:
    def test_info_findings_are_not_alerted(self, server):
        router = AlertRouter(make_alerts(), now=DAYTIME)
        decision = router.decide(make_finding(severity="info", category="index"), server)
        assert not decision.sent and "recorded, not alerted" in decision.suppressed_reason

    def test_index_warnings_are_recorded_not_paged(self, server):
        # Even at warn severity, index findings stay in mon.findings.
        router = AlertRouter(make_alerts(), now=DAYTIME)
        decision = router.decide(make_finding(severity="warn", category="index"), server)
        assert not decision.sent and "not paged" in decision.suppressed_reason

    def test_space_criticals_are_alerted(self, server):
        router = AlertRouter(make_alerts(), now=DAYTIME)
        assert router.decide(make_finding(), server).sent


class TestRouting:
    def test_only_enabled_channels_survive(self, server):
        # Routing names teams, but only email is enabled in config.
        router = AlertRouter(make_alerts(), now=DAYTIME)
        assert router.decide(make_finding(), server).channels == ["email"]

    def test_tag_routing_is_honoured(self):
        alerts = make_alerts(
            channels={"email": {"enabled": True, "smtp_host": "s", "from": "a@b.c", "to": ["d@b.c"]},
                      "slack": {"enabled": True, "webhook_ref": "env:SLACK_WEBHOOK"}},
            by_tag={"tier1": {"routing": {"crit": ["email", "slack"]}}},
        )
        router = AlertRouter(alerts, now=DAYTIME)
        tier1 = ServerConfig(name="PRD-SQL-01", host="h", tags=["tier1"])
        tier2 = ServerConfig(name="PRD-SQL-02", host="h", tags=["tier2"])

        assert router.decide(make_finding(), tier1).channels == ["email", "slack"]
        assert router.decide(make_finding(), tier2).channels == ["email"]

    def test_no_route_for_the_severity(self, server):
        router = AlertRouter(make_alerts(routing={"crit": []}), now=DAYTIME)
        decision = router.decide(make_finding(), server)
        assert not decision.sent and "no channel routed" in decision.suppressed_reason


class TestCooldown:
    def test_the_same_finding_is_not_resent_within_the_window(self, server):
        # A disk that has been 90% full since Tuesday should not page every 15 minutes.
        repo = FakeRepo(last_alert=datetime.utcnow() - timedelta(minutes=10))
        router = AlertRouter(make_alerts(), repository=repo, now=DAYTIME)
        decision = router.decide(make_finding(), server)
        assert not decision.sent and "cooldown" in decision.suppressed_reason

    def test_it_is_resent_once_the_window_has_passed(self, server):
        repo = FakeRepo(last_alert=datetime.utcnow() - timedelta(minutes=90))
        router = AlertRouter(make_alerts(), repository=repo, now=DAYTIME)
        assert router.decide(make_finding(), server).sent

    def test_a_never_seen_finding_is_sent(self, server):
        router = AlertRouter(make_alerts(), repository=FakeRepo(None), now=DAYTIME)
        assert router.decide(make_finding(), server).sent

    def test_a_repository_failure_does_not_silence_alerts(self, server):
        class BrokenRepo(FakeRepo):
            def last_alert_time(self, fingerprint):
                raise RuntimeError("repository unavailable")

        router = AlertRouter(make_alerts(), repository=BrokenRepo(), now=DAYTIME)
        # Failing open is right here: a missed page is worse than a duplicate one.
        assert router.decide(make_finding(), server).sent


class TestQuietHours:
    def test_warnings_are_held_overnight(self, server):
        router = AlertRouter(make_alerts(), now=NIGHT)
        decision = router.decide(make_finding(severity="warn", category="cpu"), server)
        assert not decision.sent and decision.suppressed_reason == "quiet hours"

    def test_criticals_still_wake_someone(self, server):
        router = AlertRouter(make_alerts(), now=NIGHT)
        assert router.decide(make_finding(severity="crit"), server).sent

    def test_criticals_can_be_held_too_when_configured(self, server):
        alerts = make_alerts(quiet_hours={"start": "22:00", "end": "06:00", "allow_crit": False})
        router = AlertRouter(alerts, now=NIGHT)
        assert not router.decide(make_finding(severity="crit"), server).sent

    def test_daytime_is_not_quiet(self, server):
        assert AlertRouter(make_alerts(), now=DAYTIME).decide(
            make_finding(severity="warn", category="cpu"), server
        ).sent


class TestDispatch:
    def test_dry_run_sends_nothing(self, server):
        repo = FakeRepo(None)
        router = AlertRouter(make_alerts(), repository=repo, now=DAYTIME)
        result = router.dispatch([make_finding()], server, 1, dry_run=True)
        assert result.delivered == 0 and repo.logged == []

    def test_a_channel_failure_is_recorded_and_contained(self, server, monkeypatch):
        repo = FakeRepo(None)
        router = AlertRouter(make_alerts(), repository=repo, now=DAYTIME)

        def explode(*args, **kwargs):
            raise RuntimeError("smtp unreachable")

        monkeypatch.setattr(router._channels["email"], "send", explode)
        result = router.dispatch([make_finding()], server, 1)

        assert result.delivered == 0
        assert "email" in result.failures
        assert repo.logged == [("PRD-SQL-01|space|db_free_pct|ERP/ERP_Data", "email", False)]


class TestFormatting:
    def test_body_carries_the_numbers_that_triggered_it(self):
        body = format_body(make_finding(), make_alerts())
        assert "Observed:  5" in body and "Threshold: 8" in body

    def test_body_names_the_server_and_the_metric(self):
        body = format_body(make_finding(), make_alerts())
        assert "PRD-SQL-01" in body and "db_free_pct" in body
