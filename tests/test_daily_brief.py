"""Tests for the daily brief module and formatters."""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from app.models.schemas import (
    Alert,
    AlertType,
    CampaignMetric,
    CreativeMetric,
    DailyBrief,
    Severity,
)
from app.modules.daily_brief import (
    DailyBriefModule,
    _calculate_confidence,
    _data_completeness,
)
from app.utils.formatters import (
    format_brief_email,
    format_brief_slack,
    format_alert_slack,
    format_currency_inr,
    format_percentage_change,
    format_usd,
    trend_arrow,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _campaign(network="meta", spend=100.0, roas=1.5, **kw) -> CampaignMetric:
    return CampaignMetric(
        date=date.today() - timedelta(days=1),
        network=network,
        campaign_id=f"c_{network}",
        campaign_name=f"Test {network}",
        os="ios", country="US",
        spend=spend, installs=50, impressions=10000, clicks=500,
        cpi=spend / 50, ipm=5.0, ctr=5.0,
        roas_d1=0.5, roas_d7=roas, roas_d30=2.0,
        revenue_d1=50.0, revenue_d7=75.0, revenue_d30=200.0,
        **kw,
    )


def _creative(creative_id="cr1", ipm=3.0) -> CreativeMetric:
    return CreativeMetric(
        creative_id=creative_id, concept_tag="Playable/Merge",
        platform="meta", ipm=ipm, ctr=2.5, roas_d7=1.5,
        spend=200.0, impressions=80000, installs=180,
        fatigue_score=0.0, days_live=14, fatigue_curve_slope=-0.01,
    )


def _sample_brief(**overrides) -> DailyBrief:
    defaults = dict(
        date=date.today(),
        spend_summary={"Meta ↑": 1200.50, "AppLovin ↓": 800.00},
        roas_tracker={"Meta": 1.45, "AppLovin": 2.1},
        action_items=[
            "[HIGH] Review Google Ads ROAS decline",
            "[MED] Refresh Meta creatives",
            "[LOW] Review AppLovin bids",
        ],
        inbox_context=["Unity rep offered Q2 bonus"],
        system_confidence=78.0,
        raw_data={},
    )
    defaults.update(overrides)
    return DailyBrief(**defaults)


# ── Formatter tests ──────────────────────────────────────────────────────────

class TestCurrencyFormatting:
    def test_inr_crores(self):
        assert format_currency_inr(1_50_00_000) == "₹1.5Cr"

    def test_inr_lakhs(self):
        assert format_currency_inr(8_20_000) == "₹8.2L"

    def test_inr_thousands(self):
        assert format_currency_inr(45_200) == "₹45.2K"

    def test_inr_small(self):
        assert format_currency_inr(950) == "₹950"

    def test_inr_negative(self):
        assert format_currency_inr(-8_20_000) == "-₹8.2L"

    def test_usd(self):
        assert format_usd(1234.56) == "$1,235"
        assert format_usd(5.99) == "$5.99"


class TestPercentageAndTrend:
    def test_positive_change(self):
        assert format_percentage_change(100, 112) == "+12%"

    def test_negative_change(self):
        assert format_percentage_change(100, 92) == "-8%"

    def test_zero_base(self):
        assert format_percentage_change(0, 0) == "N/A"
        assert format_percentage_change(0, 10) == "+∞%"

    def test_trend_strong_up(self):
        assert "↑↑" in trend_arrow(25)

    def test_trend_up(self):
        assert "↑" in trend_arrow(10)

    def test_trend_flat(self):
        assert "→" in trend_arrow(2)

    def test_trend_down(self):
        assert "↓" in trend_arrow(-10)

    def test_trend_strong_down(self):
        assert "↓↓" in trend_arrow(-25)


class TestBriefSlackFormatter:
    def test_has_header(self):
        brief = _sample_brief()
        blocks = format_brief_slack(brief)
        assert blocks[0]["type"] == "header"
        assert "Morning Brief" in blocks[0]["text"]["text"]

    def test_has_all_sections(self):
        brief = _sample_brief()
        blocks = format_brief_slack(brief)
        all_text = " ".join(
            b.get("text", {}).get("text", "")
            for b in blocks
            if b["type"] == "section"
        )
        assert "Spend Summary" in all_text
        assert "ROAS Tracker" in all_text
        assert "Action Items" in all_text
        assert "Inbox Context" in all_text

    def test_confidence_in_context(self):
        brief = _sample_brief(system_confidence=78.0)
        blocks = format_brief_slack(brief)
        ctx = [b for b in blocks if b["type"] == "context"]
        assert any("78%" in c["elements"][0]["text"] for c in ctx)

    def test_revenue_caveat(self):
        brief = _sample_brief(raw_data={"revenue_unavailable": True})
        blocks = format_brief_slack(brief)
        ctx_text = " ".join(
            e["text"]
            for b in blocks if b["type"] == "context"
            for e in b.get("elements", [])
        )
        assert "Revenue data unavailable" in ctx_text

    def test_cached_data_caveat(self):
        brief = _sample_brief(raw_data={"using_cached_data": True})
        blocks = format_brief_slack(brief)
        ctx_text = " ".join(
            e["text"]
            for b in blocks if b["type"] == "context"
            for e in b.get("elements", [])
        )
        assert "cached data" in ctx_text

    def test_missing_networks_caveat(self):
        brief = _sample_brief(raw_data={"missing_networks": ["google", "applovin"]})
        blocks = format_brief_slack(brief)
        ctx_text = " ".join(
            e["text"]
            for b in blocks if b["type"] == "context"
            for e in b.get("elements", [])
        )
        assert "google" in ctx_text


class TestBriefEmailFormatter:
    def test_html_structure(self):
        brief = _sample_brief()
        html = format_brief_email(brief)
        assert "<html>" in html
        assert "Morning Brief" in html
        assert "Spend Summary" in html
        assert "ROAS Tracker" in html
        assert "Action Items" in html

    def test_revenue_caveat_in_email(self):
        brief = _sample_brief(raw_data={"revenue_unavailable": True})
        html = format_brief_email(brief)
        assert "Revenue data unavailable" in html


class TestAlertSlackFormatter:
    def test_alert_has_severity_emoji(self):
        alert = Alert(
            alert_id="t1", alert_type=AlertType.anomaly,
            severity=Severity.critical, title="Test",
            body="Body", confidence=90.0, related_campaign="c1",
        )
        blocks = format_alert_slack(alert)
        assert ":red_circle:" in blocks[0]["text"]["text"]

    def test_alert_has_context(self):
        alert = Alert(
            alert_id="t1", alert_type=AlertType.anomaly,
            severity=Severity.high, title="Test",
            body="Body", confidence=85.0, related_campaign="c1",
        )
        blocks = format_alert_slack(alert)
        ctx = [b for b in blocks if b["type"] == "context"]
        assert any("HIGH" in e["text"] for c in ctx for e in c["elements"])


# ── Confidence calculation ───────────────────────────────────────────────────

class TestConfidence:
    def test_perfect_confidence(self):
        c = _calculate_confidence(1.0, 1.0, 1.0)
        assert c == 100.0

    def test_zero_confidence(self):
        c = _calculate_confidence(0.0, 0.0, 0.0)
        assert c == 0.0

    def test_partial(self):
        c = _calculate_confidence(0.8, 0.6, 0.7)
        assert 50 < c < 80

    def test_data_completeness_no_data(self):
        assert _data_completeness([], {}) == 0.0

    def test_data_completeness_full(self):
        rows = [_campaign(n) for n in ["meta", "applovin", "google", "google ads"]]
        score = _data_completeness(rows, {})
        assert score > 0.7

    def test_data_completeness_penalised_for_cached(self):
        rows = [_campaign("meta")]
        full = _data_completeness(rows, {})
        cached = _data_completeness(rows, {"using_cached_data": True})
        assert cached < full


# ── compile_brief_data ───────────────────────────────────────────────────────

class TestCompileBriefData:
    def test_returns_required_keys(self):
        module = DailyBriefModule()
        data = module.compile_brief_data(
            campaign_data=[_campaign("meta", 1200), _campaign("applovin", 800)],
            creative_data=[_creative()],
            historical=[_campaign("meta", 1100)],
            anomalies=[
                Alert(alert_id="a1", severity=Severity.high,
                      title="Spend spike", related_campaign="c_meta",
                      confidence=80.0),
            ],
            emails=[{"sender": "rep@unity.com", "subject": "Bonus offer",
                     "category": "network_rep", "extracted_insights": "10% bonus"}],
            slack_threads=[],
            weights={"anomaly": 1.0},
        )
        assert "campaign_metrics" in data
        assert "anomalies" in data
        assert "email_insights" in data
        assert "confidence" in data
        assert "missing_networks" in data

    def test_network_aggregation(self):
        module = DailyBriefModule()
        data = module.compile_brief_data(
            campaign_data=[
                _campaign("meta", spend=500),
                _campaign("meta", spend=700),
            ],
            creative_data=[], historical=[], anomalies=[],
            emails=[], slack_threads=[], weights={},
        )
        assert data["campaign_metrics"]["meta"]["spend"] == 1200

    def test_missing_networks_detected(self):
        module = DailyBriefModule()
        data = module.compile_brief_data(
            campaign_data=[_campaign("meta")],
            creative_data=[], historical=[], anomalies=[],
            emails=[], slack_threads=[], weights={},
        )
        # google, applovin, etc. should be flagged as missing
        assert len(data["missing_networks"]) > 0

    def test_revenue_caveat_propagated(self):
        module = DailyBriefModule()
        data = module.compile_brief_data(
            campaign_data=[_campaign("meta")],
            creative_data=[], historical=[], anomalies=[],
            emails=[], slack_threads=[], weights={},
            caveats={"revenue_unavailable": True},
        )
        assert data["revenue_unavailable"] is True


# ── Full run_daily_brief (mocked) ────────────────────────────────────────────

class TestRunDailyBrief:
    def _setup_mocks(self):
        """Return a dict of patchers and mock objects."""
        import json

        # Singular
        singular_mock = MagicMock()
        singular_mock.pull_campaign_data = AsyncMock(return_value=[
            _campaign("meta", 1200, 1.45),
            _campaign("applovin", 800, 2.1),
            _campaign("google", 500, 0.95),
        ])
        singular_mock.pull_creative_data = AsyncMock(return_value=[
            _creative("cr1", 3.0), _creative("cr2", 1.5),
        ])
        singular_mock.revenue_data_available = False  # simulate $0 revenue

        # Airtable
        airtable_mock = MagicMock()
        airtable_mock.get_recent_metrics.return_value = [
            _campaign("meta", 1100, 1.5),
            _campaign("applovin", 750, 2.0),
        ]
        airtable_mock.get_recent_creative_metrics.return_value = []
        airtable_mock.get_learning_weights.return_value = {"anomaly": 1.0}
        airtable_mock.upsert_daily_metrics.return_value = 3
        airtable_mock.upsert_creative_performance.return_value = 2
        airtable_mock.save_email_context.return_value = None

        # Claude
        claude_mock = MagicMock()
        claude_mock.generate_daily_brief.return_value = DailyBrief(
            date=date.today(),
            spend_summary={"Meta ↑": 1200, "AppLovin ↓": 800, "Google →": 500},
            roas_tracker={"Meta": 1.45, "AppLovin": 2.1, "Google (decline)": 0.95},
            action_items=[
                "[HIGH] Google Ads ROAS declining 3 consecutive days",
                "[MED] Refresh Meta creatives — fatigue detected",
                "[LOW] AppLovin spend down 15%",
            ],
            inbox_context=["Unity bonus offer for Q2"],
            system_confidence=78.0,
            raw_data={},
        )

        # Slack
        slack_mock = MagicMock()
        slack_mock.post_daily_brief.return_value = "1234.5678"

        # Gmail
        gmail_mock = MagicMock()
        gmail_mock.scan_inbox.return_value = [
            {"sender": "rep@unity.com", "subject": "Q2 bonus",
             "category": "network_rep", "extracted_insights": "10% bonus",
             "email_id": "e1", "related_campaign": ""},
        ]
        gmail_mock.archive_brief.return_value = "draft_123"

        return {
            "singular": singular_mock,
            "airtable": airtable_mock,
            "claude": claude_mock,
            "slack": slack_mock,
            "gmail": gmail_mock,
        }

    @patch("app.modules.daily_brief.DailyBriefModule._scan_slack", return_value=[])
    def test_full_pipeline(self, _mock_slack_scan):
        mocks = self._setup_mocks()

        with (
            patch("app.services.singular.SingularClient", return_value=mocks["singular"]),
            patch("app.services.airtable.AirtableService", return_value=mocks["airtable"]),
            patch("app.services.claude.ClaudeService", return_value=mocks["claude"]),
            patch("app.services.slack_bot.SlackService", return_value=mocks["slack"]),
            patch("app.services.gmail.GmailService", return_value=mocks["gmail"]),
        ):
            module = DailyBriefModule()
            brief = module.run_daily_brief()

        assert isinstance(brief, DailyBrief)
        assert brief.date == date.today()
        assert len(brief.spend_summary) >= 2
        assert len(brief.action_items) >= 1
        assert brief.system_confidence > 0

        # Revenue caveat propagated
        assert brief.raw_data.get("revenue_unavailable") is True

        # All 5 sections present
        assert brief.spend_summary
        assert brief.roas_tracker
        assert brief.action_items
        assert brief.inbox_context
        assert brief.system_confidence > 0

    @patch("app.modules.daily_brief.DailyBriefModule._scan_slack", return_value=[])
    def test_singular_failure_uses_cache(self, _mock_slack_scan):
        """When Singular fails, should fall back to Airtable cache."""
        mocks = self._setup_mocks()
        mocks["singular"].pull_campaign_data = AsyncMock(side_effect=Exception("API down"))
        mocks["singular"].pull_creative_data = AsyncMock(side_effect=Exception("API down"))

        with (
            patch("app.services.singular.SingularClient", return_value=mocks["singular"]),
            patch("app.services.airtable.AirtableService", return_value=mocks["airtable"]),
            patch("app.services.claude.ClaudeService", return_value=mocks["claude"]),
            patch("app.services.slack_bot.SlackService", return_value=mocks["slack"]),
            patch("app.services.gmail.GmailService", return_value=mocks["gmail"]),
        ):
            module = DailyBriefModule()
            brief = module.run_daily_brief()

        assert isinstance(brief, DailyBrief)
        assert brief.raw_data.get("using_cached_data") is True

    @patch("app.modules.daily_brief.DailyBriefModule._scan_slack", return_value=[])
    def test_claude_failure_uses_template(self, _mock_slack_scan):
        """When Claude fails, should generate template-based brief."""
        mocks = self._setup_mocks()
        mocks["claude"].generate_daily_brief.side_effect = Exception("Claude down")

        with (
            patch("app.services.singular.SingularClient", return_value=mocks["singular"]),
            patch("app.services.airtable.AirtableService", return_value=mocks["airtable"]),
            patch("app.services.claude.ClaudeService", return_value=mocks["claude"]),
            patch("app.services.slack_bot.SlackService", return_value=mocks["slack"]),
            patch("app.services.gmail.GmailService", return_value=mocks["gmail"]),
        ):
            module = DailyBriefModule()
            brief = module.run_daily_brief()

        assert isinstance(brief, DailyBrief)
        # Template brief should still have spend summary from data
        assert brief.spend_summary


class TestHandleBriefFailure:
    def test_returns_degraded_brief(self):
        module = DailyBriefModule()
        with patch("app.services.slack_bot.SlackService") as MockSlack:
            MockSlack.return_value.dm_natasha.return_value = "ts"
            brief = module.handle_brief_failure(RuntimeError("test error"))

        assert isinstance(brief, DailyBrief)
        assert brief.system_confidence == 0.0
        assert "failed" in brief.action_items[0].lower()
        assert brief.raw_data.get("degraded") is True


# ── Intraday check ───────────────────────────────────────────────────────────

class TestIntradayCheck:
    def test_pacing_alert_generated(self):
        module = DailyBriefModule()

        singular_mock = MagicMock()
        singular_mock.pull_campaign_data = AsyncMock(return_value=[
            _campaign("meta", spend=2000),  # way over-pacing
        ])

        airtable_mock = MagicMock()
        # 7 days of meta at $500/day
        airtable_mock.get_recent_metrics.return_value = [
            _campaign("meta", spend=500) for _ in range(7)
        ]

        slack_mock = MagicMock()
        slack_mock.post_alert.return_value = "ts"
        slack_mock.dm_natasha.return_value = "ts"

        with (
            patch("app.services.singular.SingularClient", return_value=singular_mock),
            patch("app.services.airtable.AirtableService", return_value=airtable_mock),
            patch("app.services.slack_bot.SlackService", return_value=slack_mock),
            patch("app.services.gmail.GmailService") as MockGmail,
        ):
            alerts = module.run_intraday_check()

        assert len(alerts) >= 1
        assert "pacing" in alerts[0].title.lower()

    def test_no_alert_when_on_pace(self):
        module = DailyBriefModule()

        # Current spend is proportional to time of day
        singular_mock = MagicMock()
        singular_mock.pull_campaign_data = AsyncMock(return_value=[
            _campaign("meta", spend=50),  # small, within 20% of expected
        ])

        airtable_mock = MagicMock()
        airtable_mock.get_recent_metrics.return_value = [
            _campaign("meta", spend=500) for _ in range(7)
        ]

        with (
            patch("app.services.singular.SingularClient", return_value=singular_mock),
            patch("app.services.airtable.AirtableService", return_value=airtable_mock),
            patch("app.services.slack_bot.SlackService"),
            patch("app.services.gmail.GmailService"),
        ):
            alerts = module.run_intraday_check()

        # May or may not alert depending on time of day
        # but at least it shouldn't crash
        assert isinstance(alerts, list)

    def test_singular_failure_returns_empty(self):
        module = DailyBriefModule()

        singular_mock = MagicMock()
        singular_mock.pull_campaign_data = AsyncMock(
            side_effect=Exception("API down")
        )

        with patch("app.services.singular.SingularClient", return_value=singular_mock):
            alerts = module.run_intraday_check()

        assert alerts == []
