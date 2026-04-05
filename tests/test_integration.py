"""
Integration tests — verify data flows across multiple modules.

All external services are mocked.  These tests validate that:
  1. The full pipeline assembles data from all sources correctly.
  2. Data flows through to Airtable storage.
  3. The brief is posted to Slack with all required sections.
  4. Feedback reactions update learning weights.
"""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest
from fastapi.testclient import TestClient

from app.models.schemas import (
    Alert,
    AlertType,
    CampaignMetric,
    CreativeMetric,
    DailyBrief,
    FeedbackEntry,
    Severity,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _mock_services(campaign_data, creative_data, emails):
    """Return a dict of mock service instances for full pipeline tests."""
    # Singular
    singular = MagicMock()
    singular.pull_campaign_data = AsyncMock(return_value=campaign_data)
    singular.pull_creative_data = AsyncMock(return_value=creative_data)
    singular.revenue_data_available = True

    # Airtable
    airtable = MagicMock()
    airtable.get_recent_metrics.return_value = campaign_data
    airtable.get_recent_creative_metrics.return_value = creative_data
    airtable.get_learning_weights.return_value = {"anomaly": 1.0, "budget": 0.8}
    airtable.upsert_daily_metrics.return_value = len(campaign_data)
    airtable.upsert_creative_performance.return_value = len(creative_data)
    airtable.save_email_context.return_value = None
    airtable.log_alert.return_value = "rec_123"
    airtable.log_feedback.return_value = None
    airtable.get_alerts_today.return_value = 0
    airtable.update_learning_weight.return_value = None

    # Claude
    claude = MagicMock()
    claude.generate_daily_brief.return_value = DailyBrief(
        date=date.today(),
        spend_summary={"AppLovin ↑": 3800, "Google →": 260, "Meta ↓": 380},
        roas_tracker={"AppLovin": 2.1, "Google": 1.5, "Meta": 0.3},
        action_items=[
            "[HIGH] Scale AppLovin to $5K/day — strong marginal ROAS",
            "[MED] Meta CPI at $14 — review creative strategy",
            "[LOW] Google Ads performing steadily",
        ],
        inbox_context=["Unity Q2 bonus offer", "Google budget alert"],
        system_confidence=78.0,
        raw_data={},
    )

    # Slack
    slack = MagicMock()
    slack.post_daily_brief.return_value = "ts_1234"
    slack.post_alert.return_value = "ts_5678"
    slack.dm_natasha.return_value = "ts_dm"

    # Gmail
    gmail = MagicMock()
    gmail.scan_inbox.return_value = emails
    gmail.archive_brief.return_value = "draft_abc"

    return {
        "singular": singular,
        "airtable": airtable,
        "claude": claude,
        "slack": slack,
        "gmail": gmail,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 1. Full pipeline with mocks
# ══════════════════════════════════════════════════════════════════════════════

class TestFullPipeline:
    @patch("app.modules.daily_brief.DailyBriefModule._scan_slack", return_value=[])
    def test_full_pipeline_produces_brief(
        self, _slack_scan, mock_campaign_data, mock_creative_data, mock_email_data
    ):
        """Full run_daily_brief produces a DailyBrief with all 5 sections."""
        mocks = _mock_services(mock_campaign_data, mock_creative_data, mock_email_data)

        with (
            patch("app.services.singular.SingularClient", return_value=mocks["singular"]),
            patch("app.services.airtable.AirtableService", return_value=mocks["airtable"]),
            patch("app.services.claude.ClaudeService", return_value=mocks["claude"]),
            patch("app.services.slack_bot.SlackService", return_value=mocks["slack"]),
            patch("app.services.gmail.GmailService", return_value=mocks["gmail"]),
        ):
            from app.modules.daily_brief import DailyBriefModule
            module = DailyBriefModule()
            brief = module.run_daily_brief()

        assert isinstance(brief, DailyBrief)
        assert brief.date is not None
        # All 5 sections present
        assert brief.spend_summary, "Missing spend_summary"
        assert brief.roas_tracker, "Missing roas_tracker"
        assert brief.action_items, "Missing action_items"
        assert brief.inbox_context, "Missing inbox_context"
        assert brief.system_confidence > 0, "Missing system_confidence"

    @patch("app.modules.daily_brief.DailyBriefModule._scan_slack", return_value=[])
    def test_pipeline_survives_singular_failure(
        self, _slack_scan, mock_campaign_data, mock_creative_data, mock_email_data
    ):
        """Pipeline degrades gracefully when Singular is down."""
        mocks = _mock_services(mock_campaign_data, mock_creative_data, mock_email_data)
        mocks["singular"].pull_campaign_data = AsyncMock(side_effect=Exception("API down"))
        mocks["singular"].pull_creative_data = AsyncMock(side_effect=Exception("API down"))

        with (
            patch("app.services.singular.SingularClient", return_value=mocks["singular"]),
            patch("app.services.airtable.AirtableService", return_value=mocks["airtable"]),
            patch("app.services.claude.ClaudeService", return_value=mocks["claude"]),
            patch("app.services.slack_bot.SlackService", return_value=mocks["slack"]),
            patch("app.services.gmail.GmailService", return_value=mocks["gmail"]),
        ):
            from app.modules.daily_brief import DailyBriefModule
            brief = DailyBriefModule().run_daily_brief()

        assert isinstance(brief, DailyBrief)
        assert brief.raw_data.get("using_cached_data") is True

    @patch("app.modules.daily_brief.DailyBriefModule._scan_slack", return_value=[])
    def test_pipeline_survives_claude_failure(
        self, _slack_scan, mock_campaign_data, mock_creative_data, mock_email_data
    ):
        """Pipeline falls back to template brief when Claude is down."""
        mocks = _mock_services(mock_campaign_data, mock_creative_data, mock_email_data)
        mocks["claude"].generate_daily_brief.side_effect = Exception("Claude down")

        with (
            patch("app.services.singular.SingularClient", return_value=mocks["singular"]),
            patch("app.services.airtable.AirtableService", return_value=mocks["airtable"]),
            patch("app.services.claude.ClaudeService", return_value=mocks["claude"]),
            patch("app.services.slack_bot.SlackService", return_value=mocks["slack"]),
            patch("app.services.gmail.GmailService", return_value=mocks["gmail"]),
        ):
            from app.modules.daily_brief import DailyBriefModule
            brief = DailyBriefModule().run_daily_brief()

        assert isinstance(brief, DailyBrief)
        # Template brief should still have some data
        assert brief.spend_summary or brief.action_items


# ══════════════════════════════════════════════════════════════════════════════
# 2. Data flows to Airtable
# ══════════════════════════════════════════════════════════════════════════════

class TestDataFlowsToAirtable:
    @patch("app.modules.daily_brief.DailyBriefModule._scan_slack", return_value=[])
    def test_campaign_data_upserted(
        self, _slack_scan, mock_campaign_data, mock_creative_data, mock_email_data
    ):
        """Campaign data from Singular gets upserted to Airtable."""
        mocks = _mock_services(mock_campaign_data, mock_creative_data, mock_email_data)

        with (
            patch("app.services.singular.SingularClient", return_value=mocks["singular"]),
            patch("app.services.airtable.AirtableService", return_value=mocks["airtable"]),
            patch("app.services.claude.ClaudeService", return_value=mocks["claude"]),
            patch("app.services.slack_bot.SlackService", return_value=mocks["slack"]),
            patch("app.services.gmail.GmailService", return_value=mocks["gmail"]),
        ):
            from app.modules.daily_brief import DailyBriefModule
            DailyBriefModule().run_daily_brief()

        mocks["airtable"].upsert_daily_metrics.assert_called_once()
        passed_data = mocks["airtable"].upsert_daily_metrics.call_args[0][0]
        assert len(passed_data) == len(mock_campaign_data)

    @patch("app.modules.daily_brief.DailyBriefModule._scan_slack", return_value=[])
    def test_email_context_saved(
        self, _slack_scan, mock_campaign_data, mock_creative_data, mock_email_data
    ):
        """Scanned emails are persisted to Airtable email_context."""
        mocks = _mock_services(mock_campaign_data, mock_creative_data, mock_email_data)

        with (
            patch("app.services.singular.SingularClient", return_value=mocks["singular"]),
            patch("app.services.airtable.AirtableService", return_value=mocks["airtable"]),
            patch("app.services.claude.ClaudeService", return_value=mocks["claude"]),
            patch("app.services.slack_bot.SlackService", return_value=mocks["slack"]),
            patch("app.services.gmail.GmailService", return_value=mocks["gmail"]),
        ):
            from app.modules.daily_brief import DailyBriefModule
            DailyBriefModule().run_daily_brief()

        # save_email_context called for each email + brief archive
        assert mocks["airtable"].save_email_context.call_count >= len(mock_email_data)


# ══════════════════════════════════════════════════════════════════════════════
# 3. Brief posts to Slack
# ══════════════════════════════════════════════════════════════════════════════

class TestBriefPostsToSlack:
    @patch("app.modules.daily_brief.DailyBriefModule._scan_slack", return_value=[])
    def test_slack_post_called(
        self, _slack_scan, mock_campaign_data, mock_creative_data, mock_email_data
    ):
        """Brief is posted to Slack #ua-daily-brief."""
        mocks = _mock_services(mock_campaign_data, mock_creative_data, mock_email_data)

        with (
            patch("app.services.singular.SingularClient", return_value=mocks["singular"]),
            patch("app.services.airtable.AirtableService", return_value=mocks["airtable"]),
            patch("app.services.claude.ClaudeService", return_value=mocks["claude"]),
            patch("app.services.slack_bot.SlackService", return_value=mocks["slack"]),
            patch("app.services.gmail.GmailService", return_value=mocks["gmail"]),
        ):
            from app.modules.daily_brief import DailyBriefModule
            DailyBriefModule().run_daily_brief()

        mocks["slack"].post_daily_brief.assert_called_once()
        posted_brief = mocks["slack"].post_daily_brief.call_args[0][0]
        assert isinstance(posted_brief, DailyBrief)

    @patch("app.modules.daily_brief.DailyBriefModule._scan_slack", return_value=[])
    def test_gmail_archive_called(
        self, _slack_scan, mock_campaign_data, mock_creative_data, mock_email_data
    ):
        """Brief is archived to Gmail with UA Briefs label."""
        mocks = _mock_services(mock_campaign_data, mock_creative_data, mock_email_data)

        with (
            patch("app.services.singular.SingularClient", return_value=mocks["singular"]),
            patch("app.services.airtable.AirtableService", return_value=mocks["airtable"]),
            patch("app.services.claude.ClaudeService", return_value=mocks["claude"]),
            patch("app.services.slack_bot.SlackService", return_value=mocks["slack"]),
            patch("app.services.gmail.GmailService", return_value=mocks["gmail"]),
        ):
            from app.modules.daily_brief import DailyBriefModule
            DailyBriefModule().run_daily_brief()

        mocks["gmail"].archive_brief.assert_called_once()


# ══════════════════════════════════════════════════════════════════════════════
# 4. Feedback updates weights
# ══════════════════════════════════════════════════════════════════════════════

class TestFeedbackUpdatesWeights:
    def test_reaction_logged_to_airtable(self):
        """process_reaction persists a FeedbackEntry to Airtable."""
        mock_at = MagicMock()
        mock_at.log_feedback.return_value = None

        with (
            patch("app.services.airtable.AirtableService", return_value=mock_at),
            patch(
                "app.pipelines.feedback.FeedbackProcessor._resolve_alert_type",
                return_value=None,
            ),
        ):
            from app.pipelines.feedback import FeedbackProcessor
            proc = FeedbackProcessor()
            proc.process_reaction("alert_123", "useful", "U_natasha")

        mock_at.log_feedback.assert_called_once()
        fb = mock_at.log_feedback.call_args[0][0]
        assert isinstance(fb, FeedbackEntry)
        assert fb.alert_id == "alert_123"
        assert fb.reaction == "useful"

    def test_weight_recalculation_updates_airtable(self):
        """recalculate_weights persists new weights to Airtable."""
        stats = {
            "anomaly": {"total": 30, "useful": 25, "noted": 3, "not_useful": 2},
            "budget": {"total": 25, "useful": 10, "noted": 5, "not_useful": 10},
        }

        mock_at = MagicMock()
        mock_at.update_learning_weight.return_value = None

        with (
            patch(
                "app.pipelines.feedback.FeedbackProcessor._get_all_stats",
                return_value=stats,
            ),
            patch("app.services.airtable.AirtableService", return_value=mock_at),
        ):
            from app.pipelines.feedback import FeedbackProcessor
            proc = FeedbackProcessor()
            weights = proc.recalculate_weights()

        assert "anomaly" in weights
        assert "budget" in weights
        assert abs(sum(weights.values()) - 1.0) < 0.01
        # anomaly (83% useful) should have higher weight than budget (40%)
        assert weights["anomaly"] > weights["budget"]
        # Weights were persisted
        assert mock_at.update_learning_weight.call_count == 2

    def test_suppression_affects_routing(self, sample_medium_alert):
        """Suppressed alert types get downgraded to LOW and are not routed."""
        from app.pipelines.feedback import FeedbackProcessor
        from app.modules.coordinator import AlertCoordinator

        proc = FeedbackProcessor()
        stats = {
            "anomaly": {"total": 35, "useful": 2, "noted": 3, "not_useful": 30},
        }
        with patch.object(proc, "_get_all_stats", return_value=stats):
            suppressed = proc.suppress_learned_patterns()

        assert "anomaly" in suppressed

        # If coordinator routes a LOW alert (which suppressed types become)
        coord = AlertCoordinator()
        low_alert = sample_medium_alert.model_copy(update={"severity": Severity.low})
        channels = coord.route_alert(low_alert)
        assert channels == []  # LOW is suppressed


# ══════════════════════════════════════════════════════════════════════════════
# 5. Cross-module data consistency
# ══════════════════════════════════════════════════════════════════════════════

class TestCrossModuleConsistency:
    def test_anomaly_output_feeds_coordinator(self, mock_campaign_data):
        """Anomaly detector output can be routed through the coordinator."""
        from app.pipelines.anomaly import AnomalyDetector
        from app.modules.coordinator import AlertCoordinator

        detector = AnomalyDetector()
        # Inject a spike
        spiked = mock_campaign_data[0].model_copy(update={"spend": 20000})
        alerts = detector.detect_campaign_anomalies(
            [spiked], mock_campaign_data, {}
        )

        if alerts:
            coord = AlertCoordinator()
            with (
                patch.object(coord, "_post_slack", return_value=True),
                patch.object(coord, "_dm_natasha", return_value=True),
                patch.object(coord, "_log_alert"),
            ):
                channels = coord.route_alert(alerts[0])
            assert len(channels) > 0

    def test_creative_analyzer_uses_conftest_data(
        self, mock_creative_data, mock_creative_history
    ):
        """Creative analyzer works with conftest fixture data."""
        from app.modules.creative import CreativeAnalyzer

        analyzer = CreativeAnalyzer()
        result = analyzer.analyze_creative_health(
            mock_creative_data, mock_creative_history
        )
        assert "scored" in result
        assert "concepts" in result
        assert result["scored"]  # Should have scored items

    def test_waterfall_optimizer_uses_conftest_data(self, mock_waterfall_data):
        """Waterfall optimizer works with conftest fixture data."""
        from app.modules.waterfall import WaterfallOptimizer

        optimizer = WaterfallOptimizer()
        analysis = optimizer.analyze_waterfall(mock_waterfall_data)
        suggestions = optimizer.generate_suggestions(analysis)
        guarded = optimizer.apply_guardrails(suggestions)

        assert analysis["summary"]["total"] == 4
        assert len(guarded) >= 2  # Unity raise + IronSource lower

    def test_compile_brief_data_uses_conftest_fixtures(
        self, mock_campaign_data, mock_email_data, mock_slack_context
    ):
        """compile_brief_data works with all conftest fixtures together."""
        from app.modules.daily_brief import DailyBriefModule

        module = DailyBriefModule()
        data = module.compile_brief_data(
            campaign_data=mock_campaign_data,
            creative_data=[],
            historical=mock_campaign_data,
            anomalies=[],
            emails=mock_email_data,
            slack_threads=mock_slack_context,
            weights={"anomaly": 1.0},
        )
        assert "campaign_metrics" in data
        assert "email_insights" in data
        assert len(data["email_insights"]) == len(mock_email_data)
        assert data["confidence"] > 0


# ══════════════════════════════════════════════════════════════════════════════
# 6. API endpoint integration
# ══════════════════════════════════════════════════════════════════════════════

class TestAPIEndpoints:
    """Test API endpoints using a fresh test client per test.

    The AsyncIOScheduler in main.py's lifespan can conflict with the
    test runner's event loop when re-entering.  We create a fresh
    scheduler for each test client to avoid the 'Event loop is closed'
    error.
    """

    @pytest.fixture(autouse=True)
    def _fresh_client(self):
        """Provide a fresh TestClient with a patched scheduler."""
        from unittest.mock import patch as _patch
        from apscheduler.schedulers.asyncio import AsyncIOScheduler as _Sched

        with _patch("app.main.scheduler", _Sched(timezone="UTC")):
            from app.main import app as _app
            with TestClient(_app) as c:
                self.client = c
                yield

    def test_health_endpoint(self):
        resp = self.client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "uptime" in data

    def test_status_endpoint(self):
        resp = self.client.get("/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "services" in data
        assert "scheduler_jobs" in data

    def test_metrics_endpoint(self):
        resp = self.client.get("/metrics")
        assert resp.status_code == 200
        assert "mergedom_uptime_seconds" in resp.text

    def test_trigger_daily_brief(self):
        resp = self.client.post("/trigger/daily-brief")
        assert resp.status_code == 200
        assert resp.json()["status"] == "triggered"

    def test_trigger_anomaly_check(self):
        resp = self.client.post("/trigger/anomaly-check")
        assert resp.status_code == 200
        assert resp.json()["status"] == "triggered"

    def test_trigger_creative_analysis(self):
        resp = self.client.post("/trigger/creative-analysis")
        assert resp.status_code == 200
        assert resp.json()["status"] == "triggered"
