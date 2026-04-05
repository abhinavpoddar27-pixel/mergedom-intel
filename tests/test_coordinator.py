"""Tests for the alert coordinator and feedback processor."""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.models.schemas import Alert, AlertType, FeedbackEntry, Severity
from app.modules.coordinator import AlertCoordinator, _CH_ALERTS, _CH_DM, _CH_DAILY_BRIEF
from app.pipelines.feedback import (
    FeedbackProcessor,
    MIN_FEEDBACKS_FOR_RECALC,
    SUPPRESSION_MIN_FEEDBACKS,
    SUPPRESSION_THRESHOLD,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _alert(
    severity: Severity = Severity.high,
    alert_type: AlertType = AlertType.anomaly,
    alert_id: str = "a1",
    campaign: str = "c1",
    confidence: float = 80.0,
) -> Alert:
    return Alert(
        alert_id=alert_id,
        alert_type=alert_type,
        severity=severity,
        title=f"Test {severity.value}",
        body="Test body",
        related_campaign=campaign,
        confidence=confidence,
        timestamp=datetime.now(tz=timezone.utc),
    )


# ══════════════════════════════════════════════════════════════════════════════
# AlertCoordinator tests
# ══════════════════════════════════════════════════════════════════════════════

class TestRouting:
    """Escalation matrix: route by severity."""

    @patch("app.modules.coordinator.AlertCoordinator._post_slack", return_value=True)
    @patch("app.modules.coordinator.AlertCoordinator._dm_natasha", return_value=True)
    @patch("app.modules.coordinator.AlertCoordinator._log_alert")
    def test_critical_goes_to_slack_and_dm(self, _log, _dm, _slack):
        coord = AlertCoordinator()
        channels = coord.route_alert(_alert(Severity.critical))
        assert _CH_ALERTS in channels
        assert _CH_DM in channels
        _dm.assert_called_once()

    @patch("app.modules.coordinator.AlertCoordinator._post_slack", return_value=True)
    @patch("app.modules.coordinator.AlertCoordinator._log_alert")
    def test_high_goes_to_slack_channel(self, _log, _slack):
        coord = AlertCoordinator()
        channels = coord.route_alert(_alert(Severity.high))
        assert _CH_ALERTS in channels
        assert "gmail_digest" in channels

    def test_medium_goes_to_daily_brief_only(self):
        coord = AlertCoordinator()
        channels = coord.route_alert(_alert(Severity.medium))
        assert channels == [_CH_DAILY_BRIEF]

    def test_low_is_suppressed(self):
        coord = AlertCoordinator()
        channels = coord.route_alert(_alert(Severity.low))
        assert channels == []

    @patch("app.modules.coordinator.AlertCoordinator._post_slack", return_value=True)
    @patch("app.modules.coordinator.AlertCoordinator._log_alert")
    def test_creative_fatigue_goes_to_creative_channel(self, _log, _slack):
        coord = AlertCoordinator()
        alert = _alert(Severity.high, AlertType.creative_fatigue)
        channels = coord.route_alert(alert)
        assert "#creative-intel" in channels

    @patch("app.modules.coordinator.AlertCoordinator._post_slack", return_value=True)
    @patch("app.modules.coordinator.AlertCoordinator._log_alert")
    def test_waterfall_goes_to_monetization_channel(self, _log, _slack):
        coord = AlertCoordinator()
        alert = _alert(Severity.high, AlertType.waterfall)
        channels = coord.route_alert(alert)
        assert "#monetization-ops" in channels


class TestDeduplication:
    @patch("app.modules.coordinator.AlertCoordinator._post_slack", return_value=True)
    @patch("app.modules.coordinator.AlertCoordinator._log_alert")
    def test_duplicate_suppressed(self, _log, _slack):
        coord = AlertCoordinator()
        a = _alert(Severity.high)
        channels1 = coord.route_alert(a)
        assert len(channels1) > 0

        # Same alert again → suppressed
        channels2 = coord.route_alert(a)
        assert channels2 == []

    @patch("app.modules.coordinator.AlertCoordinator._post_slack", return_value=True)
    @patch("app.modules.coordinator.AlertCoordinator._log_alert")
    def test_different_alerts_not_deduped(self, _log, _slack):
        coord = AlertCoordinator()
        a1 = _alert(Severity.high, alert_id="a1", campaign="c1")
        a2 = _alert(Severity.high, alert_id="a2", campaign="c2")
        assert len(coord.route_alert(a1)) > 0
        assert len(coord.route_alert(a2)) > 0


class TestEscalation:
    @patch("app.modules.coordinator.AlertCoordinator._post_slack", return_value=True)
    @patch("app.modules.coordinator.AlertCoordinator._dm_natasha", return_value=True)
    @patch("app.modules.coordinator.AlertCoordinator._log_alert")
    @patch("app.modules.coordinator.AlertCoordinator._has_feedback", return_value=False)
    @patch("app.modules.coordinator.AlertCoordinator._send_gmail_escalation")
    def test_escalates_after_timeout(self, _gmail, _fb, _log, _dm, _slack):
        coord = AlertCoordinator()
        alert = _alert(Severity.critical)
        coord.route_alert(alert)

        # Simulate 3 hours elapsed
        coord._pending_escalation[alert.alert_id] = (
            alert,
            datetime.now(tz=timezone.utc) - timedelta(hours=3),
        )

        result = coord.escalate_if_needed(alert, hours_since=2)
        assert result is True
        _gmail.assert_called_once()

    @patch("app.modules.coordinator.AlertCoordinator._post_slack", return_value=True)
    @patch("app.modules.coordinator.AlertCoordinator._dm_natasha", return_value=True)
    @patch("app.modules.coordinator.AlertCoordinator._log_alert")
    @patch("app.modules.coordinator.AlertCoordinator._has_feedback", return_value=True)
    def test_no_escalation_when_reacted(self, _fb, _log, _dm, _slack):
        coord = AlertCoordinator()
        alert = _alert(Severity.critical)
        coord.route_alert(alert)

        coord._pending_escalation[alert.alert_id] = (
            alert,
            datetime.now(tz=timezone.utc) - timedelta(hours=3),
        )

        result = coord.escalate_if_needed(alert, hours_since=2)
        assert result is False

    def test_no_escalation_before_timeout(self):
        coord = AlertCoordinator()
        alert = _alert(Severity.critical)
        coord._pending_escalation[alert.alert_id] = (
            alert,
            datetime.now(tz=timezone.utc) - timedelta(minutes=30),
        )
        result = coord.escalate_if_needed(alert, hours_since=2)
        assert result is False


class TestGmailDigest:
    @patch("app.modules.coordinator.AlertCoordinator._has_feedback")
    @patch("app.modules.coordinator.AlertCoordinator.check_slack_discussed")
    def test_digest_structure(self, mock_discussed, mock_fb):
        mock_fb.return_value = False
        mock_discussed.return_value = False

        coord = AlertCoordinator()
        alerts = [
            _alert(Severity.critical, alert_id="a1"),
            _alert(Severity.high, alert_id="a2"),
        ]
        digest = coord.compile_gmail_digest(alerts)
        assert digest["total_alerts"] == 2
        assert len(digest["sections"]["critical"]) == 1
        assert len(digest["sections"]["high"]) == 1

    @patch("app.modules.coordinator.AlertCoordinator._has_feedback", return_value=True)
    @patch("app.modules.coordinator.AlertCoordinator.check_slack_discussed")
    def test_acknowledged_alerts_in_resolved(self, mock_disc, mock_fb):
        coord = AlertCoordinator()
        alerts = [_alert(Severity.high, alert_id="a1")]
        digest = coord.compile_gmail_digest(alerts)
        assert digest["acknowledged_count"] == 1
        assert len(digest["sections"]["resolved"]) == 1

    @patch("app.modules.coordinator.AlertCoordinator._has_feedback", return_value=False)
    @patch("app.modules.coordinator.AlertCoordinator.check_slack_discussed", return_value=True)
    def test_slack_discussed_noted(self, mock_disc, mock_fb):
        coord = AlertCoordinator()
        alerts = [_alert(Severity.high, alert_id="a1")]
        digest = coord.compile_gmail_digest(alerts)
        entry = digest["sections"]["high"][0]
        assert "discussed" in entry.get("note", "").lower()


# ══════════════════════════════════════════════════════════════════════════════
# FeedbackProcessor tests
# ══════════════════════════════════════════════════════════════════════════════

class TestPriorityScore:
    def test_critical_high_weight_high_score(self):
        proc = FeedbackProcessor()
        alert = _alert(Severity.critical, confidence=90)
        score = proc.get_priority_score(alert, {"anomaly": 1.5})
        assert score > 3.0

    def test_low_severity_low_score(self):
        proc = FeedbackProcessor()
        alert = _alert(Severity.low, confidence=50)
        score = proc.get_priority_score(alert, {"anomaly": 0.5})
        assert score < 1.0

    def test_weight_amplifies_score(self):
        proc = FeedbackProcessor()
        alert = _alert(Severity.high, confidence=80)
        low_w = proc.get_priority_score(alert, {"anomaly": 0.3})
        high_w = proc.get_priority_score(alert, {"anomaly": 1.5})
        assert high_w > low_w

    def test_missing_weight_defaults_to_1(self):
        proc = FeedbackProcessor()
        alert = _alert(Severity.high, confidence=80)
        score = proc.get_priority_score(alert, {})
        assert score > 0


class TestRecalculateWeights:
    def _mock_stats(self, stats_by_type: dict):
        """Patch _get_all_stats to return given stats."""
        return patch.object(
            FeedbackProcessor, "_get_all_stats", return_value=stats_by_type
        )

    def test_high_positive_rate_higher_weight(self):
        proc = FeedbackProcessor()
        stats = {
            "anomaly": {"total": 30, "useful": 25, "noted": 3, "not_useful": 2},
            "creative_fatigue": {"total": 30, "useful": 5, "noted": 5, "not_useful": 20},
        }
        with self._mock_stats(stats), \
             patch("app.services.airtable.AirtableService"):
            weights = proc.recalculate_weights()

        assert weights["anomaly"] > weights["creative_fatigue"]

    def test_weights_sum_to_one(self):
        proc = FeedbackProcessor()
        stats = {
            "anomaly": {"total": 20, "useful": 15, "noted": 3, "not_useful": 2},
            "budget": {"total": 20, "useful": 10, "noted": 5, "not_useful": 5},
        }
        with self._mock_stats(stats), \
             patch("app.services.airtable.AirtableService"):
            weights = proc.recalculate_weights()

        assert abs(sum(weights.values()) - 1.0) < 0.01

    def test_empty_stats_returns_empty(self):
        proc = FeedbackProcessor()
        with self._mock_stats({}):
            weights = proc.recalculate_weights()
        assert weights == {}

    def test_zero_total_defaults_to_even(self):
        proc = FeedbackProcessor()
        stats = {
            "a": {"total": 0, "useful": 0, "noted": 0, "not_useful": 0},
            "b": {"total": 0, "useful": 0, "noted": 0, "not_useful": 0},
        }
        with self._mock_stats(stats), \
             patch("app.services.airtable.AirtableService"):
            weights = proc.recalculate_weights()
        # Both should get equal weight
        assert abs(weights["a"] - weights["b"]) < 0.01


class TestSuppression:
    def test_suppresses_low_positive_rate(self):
        """Alert type with <10% positive rate after 30+ feedbacks → suppressed."""
        proc = FeedbackProcessor()
        stats = {
            "weekend_spend_dip": {
                "total": 35, "useful": 3, "noted": 2, "not_useful": 30
            },
        }
        with patch.object(proc, "_get_all_stats", return_value=stats):
            suppressed = proc.suppress_learned_patterns()
        assert "weekend_spend_dip" in suppressed

    def test_does_not_suppress_healthy_type(self):
        proc = FeedbackProcessor()
        stats = {
            "anomaly": {"total": 40, "useful": 30, "noted": 5, "not_useful": 5},
        }
        with patch.object(proc, "_get_all_stats", return_value=stats):
            suppressed = proc.suppress_learned_patterns()
        assert "anomaly" not in suppressed

    def test_does_not_suppress_below_min_feedbacks(self):
        """Even with 0% positive, don't suppress if <30 feedbacks."""
        proc = FeedbackProcessor()
        stats = {
            "new_type": {"total": 10, "useful": 0, "noted": 0, "not_useful": 10},
        }
        with patch.object(proc, "_get_all_stats", return_value=stats):
            suppressed = proc.suppress_learned_patterns()
        assert suppressed == []

    def test_exactly_at_threshold_suppressed(self):
        """Exactly 10% positive rate with 30 feedbacks → right at boundary."""
        proc = FeedbackProcessor()
        stats = {
            "borderline": {"total": 30, "useful": 3, "noted": 0, "not_useful": 27},
        }
        with patch.object(proc, "_get_all_stats", return_value=stats):
            suppressed = proc.suppress_learned_patterns()
        # 3/30 = 10% = exactly at threshold — NOT suppressed (must be < 10%)
        assert suppressed == []

    def test_just_below_threshold_suppressed(self):
        """9.6% positive with 31 feedbacks → suppressed."""
        proc = FeedbackProcessor()
        stats = {
            "noisy": {"total": 31, "useful": 2, "noted": 1, "not_useful": 28},
        }
        with patch.object(proc, "_get_all_stats", return_value=stats):
            suppressed = proc.suppress_learned_patterns()
        # 2/31 ≈ 6.5% < 10%
        assert "noisy" in suppressed


class TestWeeklyReport:
    def test_report_structure(self):
        proc = FeedbackProcessor()
        stats = {
            "anomaly": {"total": 25, "useful": 20, "noted": 3, "not_useful": 2},
            "budget": {"total": 15, "useful": 5, "noted": 5, "not_useful": 5},
        }
        with patch.object(proc, "_get_all_stats", return_value=stats), \
             patch("app.services.airtable.AirtableService"), \
             patch.object(proc, "_post_report"):
            report = proc.weekly_accuracy_report()

        assert "week_ending" in report
        assert "overall_useful_rate" in report
        assert "per_type" in report
        assert "weights" in report
        assert report["total_alerts"] == 40
        assert report["total_useful"] == 25

    def test_report_posts_to_slack(self):
        proc = FeedbackProcessor()
        stats = {"anomaly": {"total": 10, "useful": 8, "noted": 1, "not_useful": 1}}
        mock_post = MagicMock()

        with patch.object(proc, "_get_all_stats", return_value=stats), \
             patch("app.services.airtable.AirtableService"), \
             patch.object(proc, "_post_report", mock_post):
            proc.weekly_accuracy_report()

        mock_post.assert_called_once()


class TestProcessReaction:
    @patch("app.services.airtable.AirtableService")
    def test_logs_feedback(self, MockAirtable):
        proc = FeedbackProcessor()
        mock_at = MockAirtable.return_value
        mock_at.log_feedback.return_value = None

        with patch.object(proc, "_resolve_alert_type", return_value=None):
            proc.process_reaction("a1", "useful", "U123")

        mock_at.log_feedback.assert_called_once()
        fb = mock_at.log_feedback.call_args[0][0]
        assert isinstance(fb, FeedbackEntry)
        assert fb.reaction == "useful"
