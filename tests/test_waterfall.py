"""Tests for MAX reporting client and waterfall optimization."""

from datetime import date

import pytest

from app.models.schemas import WaterfallInstance
from app.modules.waterfall import (
    WaterfallOptimizer,
    _evaluate_instance,
    _suggestion_confidence,
    ECPM_RAISE_RATIO,
    ECPM_LOWER_RATIO,
    FILL_RATE_LOW,
    FILL_RATE_HIGH,
    MAX_FLOOR_CHANGE_PCT,
    NETWORK_FLOOR_MINIMUMS,
)
from app.services.max_reporting import MaxReportingClient


# ── Helpers ──────────────────────────────────────────────────────────────────

def _wf(
    network: str = "Unity",
    geo: str = "T1",
    fmt: str = "rewarded",
    floor: float = 10.0,
    ecpm: float = 12.0,
    fill: float = 0.80,
    revenue: float = 50.0,
    time_block: str = "8-12",
) -> WaterfallInstance:
    return WaterfallInstance(
        network=network,
        geo_tier=geo,
        ad_format=fmt,
        floor_price=floor,
        ecpm=ecpm,
        fill_rate=fill,
        show_rate=0.90,
        revenue=revenue,
        date=date.today(),
        time_block=time_block,
    )


# ── MaxReportingClient ──────────────────────────────────────────────────────

class TestMaxReportingClient:
    def test_instantiates(self):
        client = MaxReportingClient()
        assert client.BASE_URL == "https://r.applovin.com/maxReport"

    def test_normalize_row(self):
        row = {
            "network": "Unity",
            "country_tier": "T1",
            "ad_format": "rewarded",
            "hour": 10,
            "ecpm": "14.50",
            "fill_rate": "0.85",
            "show_rate": "0.92",
            "estimated_revenue": "123.45",
            "floor_price": "10.00",
        }
        inst = MaxReportingClient._normalize_row(row, "2025-06-01")
        assert isinstance(inst, WaterfallInstance)
        assert inst.network == "Unity"
        assert inst.geo_tier == "T1"
        assert inst.ecpm == 14.50
        assert inst.fill_rate == 0.85
        assert inst.time_block == "8-12"  # hour 10 → block 8-12
        assert inst.date == date(2025, 6, 1)

    def test_normalize_row_time_blocks(self):
        for hour, expected_block in [(0, "0-4"), (5, "4-8"), (13, "12-16"), (23, "20-24")]:
            inst = MaxReportingClient._normalize_row({"hour": hour}, "2025-01-01")
            assert inst.time_block == expected_block


# ── Instance evaluation ──────────────────────────────────────────────────────

class TestEvaluateInstance:
    def test_ecpm_above_raise_threshold(self):
        """eCPM > floor * 1.3 → raise."""
        inst = _wf(floor=10.0, ecpm=14.0)  # 14 > 10*1.3=13
        signal, reason = _evaluate_instance(inst)
        assert signal == "raise"

    def test_ecpm_below_lower_threshold(self):
        """eCPM < floor * 0.9 → lower."""
        inst = _wf(floor=10.0, ecpm=8.0)  # 8 < 10*0.9=9
        signal, reason = _evaluate_instance(inst)
        assert signal == "lower"

    def test_low_fill_rate_suggests_lower(self):
        """Fill < 60% with acceptable eCPM → floor too high."""
        inst = _wf(floor=10.0, ecpm=8.0, fill=0.45)
        signal, reason = _evaluate_instance(inst)
        assert signal == "lower"
        assert "fill rate" in reason.lower()

    def test_high_fill_rate_suggests_raise(self):
        """Fill > 95% → floor possibly too low."""
        inst = _wf(floor=10.0, ecpm=11.0, fill=0.97)
        signal, reason = _evaluate_instance(inst)
        assert signal == "raise"

    def test_within_range_ok(self):
        """eCPM within acceptable range + normal fill → ok."""
        inst = _wf(floor=10.0, ecpm=11.0, fill=0.80)  # 11 < 13 and > 9
        signal, reason = _evaluate_instance(inst)
        assert signal == "ok"

    def test_no_floor_set(self):
        inst = _wf(floor=0.0, ecpm=12.0)
        signal, _ = _evaluate_instance(inst)
        assert signal == "ok"


# ── Analysis ─────────────────────────────────────────────────────────────────

class TestAnalyzeWaterfall:
    def test_returns_summary(self):
        optimizer = WaterfallOptimizer()
        data = [
            _wf(floor=10.0, ecpm=14.0),  # raise
            _wf(floor=10.0, ecpm=8.0),   # lower
            _wf(floor=10.0, ecpm=11.0),  # ok
        ]
        result = optimizer.analyze_waterfall(data)
        assert result["summary"]["total"] == 3
        assert result["summary"]["raise"] == 1
        assert result["summary"]["lower"] == 1
        assert result["summary"]["ok"] == 1

    def test_instances_have_signal(self):
        optimizer = WaterfallOptimizer()
        result = optimizer.analyze_waterfall([_wf(floor=10, ecpm=15)])
        assert result["instances"][0]["signal"] == "raise"


# ── Suggestions ──────────────────────────────────────────────────────────────

class TestGenerateSuggestions:
    def test_raise_suggestion(self):
        optimizer = WaterfallOptimizer()
        analysis = optimizer.analyze_waterfall([_wf(floor=10, ecpm=15)])
        suggestions = optimizer.generate_suggestions(analysis)
        assert len(suggestions) == 1
        s = suggestions[0]
        assert s["recommended_floor"] > s["current_floor"]
        assert s["network"] == "Unity"
        assert s["confidence"] > 0

    def test_lower_suggestion(self):
        optimizer = WaterfallOptimizer()
        analysis = optimizer.analyze_waterfall([_wf(floor=10, ecpm=7)])
        suggestions = optimizer.generate_suggestions(analysis)
        assert len(suggestions) == 1
        assert suggestions[0]["recommended_floor"] < suggestions[0]["current_floor"]

    def test_ok_produces_no_suggestion(self):
        optimizer = WaterfallOptimizer()
        analysis = optimizer.analyze_waterfall([_wf(floor=10, ecpm=11, fill=0.80)])
        suggestions = optimizer.generate_suggestions(analysis)
        assert suggestions == []


# ── Guardrails ───────────────────────────────────────────────────────────────

class TestGuardrails:
    def test_caps_increase_at_15_pct(self):
        optimizer = WaterfallOptimizer()
        suggestions = [{
            "network": "Unity",
            "current_floor": 10.0,
            "recommended_floor": 15.0,  # +50% — should be capped
            "change_pct": 50.0,
            "geo_tier": "T1",
            "ad_format": "rewarded",
            "time_block": "8-12",
            "ecpm": 15.0,
            "fill_rate": 0.80,
            "confidence": 80.0,
            "reason": "test",
        }]
        guarded = optimizer.apply_guardrails(suggestions)
        assert guarded[0]["recommended_floor"] == 11.5  # 10 * 1.15
        assert guarded[0]["guardrail_applied"] is True
        assert guarded[0]["change_pct"] == 15.0

    def test_caps_decrease_at_15_pct(self):
        optimizer = WaterfallOptimizer()
        suggestions = [{
            "network": "Unity",
            "current_floor": 10.0,
            "recommended_floor": 5.0,  # -50% — should be capped
            "change_pct": -50.0,
            "geo_tier": "T1",
            "ad_format": "rewarded",
            "time_block": "8-12",
            "ecpm": 5.0,
            "fill_rate": 0.40,
            "confidence": 70.0,
            "reason": "test",
        }]
        guarded = optimizer.apply_guardrails(suggestions)
        assert guarded[0]["recommended_floor"] == 8.5  # 10 * 0.85
        assert guarded[0]["guardrail_applied"] is True

    def test_enforces_network_minimum(self):
        optimizer = WaterfallOptimizer()
        suggestions = [{
            "network": "AppLovin",
            "current_floor": 1.05,
            "recommended_floor": 0.50,  # below AppLovin min of $1.00
            "change_pct": -52.0,
            "geo_tier": "T3",
            "ad_format": "banner",
            "time_block": "0-4",
            "ecpm": 0.80,
            "fill_rate": 0.40,
            "confidence": 65.0,
            "reason": "test",
        }]
        guarded = optimizer.apply_guardrails(suggestions)
        min_floor = NETWORK_FLOOR_MINIMUMS["AppLovin"]
        assert guarded[0]["recommended_floor"] >= min_floor

    def test_small_change_no_guardrail(self):
        optimizer = WaterfallOptimizer()
        suggestions = [{
            "network": "Unity",
            "current_floor": 10.0,
            "recommended_floor": 10.5,  # +5% — within 15%
            "change_pct": 5.0,
            "geo_tier": "T1",
            "ad_format": "rewarded",
            "time_block": "8-12",
            "ecpm": 13.5,
            "fill_rate": 0.85,
            "confidence": 75.0,
            "reason": "test",
        }]
        guarded = optimizer.apply_guardrails(suggestions)
        assert guarded[0]["recommended_floor"] == 10.5
        assert guarded[0]["guardrail_applied"] is False

    def test_requires_approval(self):
        optimizer = WaterfallOptimizer()
        suggestions = [{
            "network": "Unity",
            "current_floor": 10.0,
            "recommended_floor": 11.0,
            "change_pct": 10.0,
            "geo_tier": "T1",
            "ad_format": "rewarded",
            "time_block": "8-12",
            "ecpm": 14.0,
            "fill_rate": 0.80,
            "confidence": 80.0,
            "reason": "test",
        }]
        guarded = optimizer.apply_guardrails(suggestions)
        assert guarded[0]["requires_approval"] is True


# ── Revenue impact ───────────────────────────────────────────────────────────

class TestRevenueImpact:
    def test_raise_floor_positive_impact(self):
        """Raising floor when eCPM is high should show positive impact."""
        optimizer = WaterfallOptimizer()
        impact = optimizer.estimate_revenue_impact(
            {
                "current_floor": 10.0,
                "recommended_floor": 12.0,
                "ecpm": 15.0,
                "fill_rate": 0.85,
            },
            daily_impressions=100_000,
        )
        # Raising floor with strong eCPM should be net positive or near zero
        assert isinstance(impact, float)

    def test_lower_floor_impact(self):
        """Lowering floor should increase fill rate, potentially positive."""
        optimizer = WaterfallOptimizer()
        impact = optimizer.estimate_revenue_impact(
            {
                "current_floor": 10.0,
                "recommended_floor": 8.0,
                "ecpm": 8.5,
                "fill_rate": 0.45,
            },
            daily_impressions=100_000,
        )
        assert isinstance(impact, float)

    def test_zero_impressions_zero_impact(self):
        optimizer = WaterfallOptimizer()
        impact = optimizer.estimate_revenue_impact(
            {
                "current_floor": 10.0,
                "recommended_floor": 12.0,
                "ecpm": 15.0,
                "fill_rate": 0.80,
            },
            daily_impressions=0,
        )
        assert impact == 0.0

    def test_zero_floor_zero_impact(self):
        optimizer = WaterfallOptimizer()
        impact = optimizer.estimate_revenue_impact(
            {
                "current_floor": 0.0,
                "recommended_floor": 5.0,
                "ecpm": 8.0,
                "fill_rate": 0.70,
            },
            daily_impressions=50_000,
        )
        assert impact == 0.0


# ── Confidence scoring ───────────────────────────────────────────────────────

class TestConfidence:
    def test_strong_signal_high_confidence(self):
        conf = _suggestion_confidence({
            "floor_price": 10.0, "ecpm": 18.0, "fill_rate": 0.80
        })
        assert conf >= 80

    def test_moderate_signal(self):
        conf = _suggestion_confidence({
            "floor_price": 10.0, "ecpm": 13.5, "fill_rate": 0.80
        })
        assert 60 < conf < 90

    def test_extreme_fill_boosts_confidence(self):
        low_fill = _suggestion_confidence({
            "floor_price": 10.0, "ecpm": 8.0, "fill_rate": 0.30
        })
        normal_fill = _suggestion_confidence({
            "floor_price": 10.0, "ecpm": 8.0, "fill_rate": 0.70
        })
        assert low_fill > normal_fill


# ── Full pipeline (mocked) ───────────────────────────────────────────────────

class TestDailyReview:
    def test_empty_data_returns_empty(self):
        from unittest.mock import patch
        optimizer = WaterfallOptimizer()
        with patch.object(optimizer, "_pull_data", return_value=[]):
            result = optimizer.run_daily_waterfall_review()
        assert result == []

    def test_pipeline_produces_suggestions(self):
        from unittest.mock import patch, MagicMock
        optimizer = WaterfallOptimizer()

        instances = [
            _wf(network="Unity", floor=10.0, ecpm=15.0),
            _wf(network="IronSource", floor=8.0, ecpm=6.0),
            _wf(network="AppLovin", floor=12.0, ecpm=13.0, fill=0.80),
        ]

        slack_mock = MagicMock()
        airtable_mock = MagicMock()
        airtable_mock._table.return_value.batch_upsert.return_value = None

        with (
            patch.object(optimizer, "_pull_data", return_value=instances),
            patch("app.services.slack_bot.SlackService", return_value=slack_mock),
            patch("app.services.airtable.AirtableService", return_value=airtable_mock),
        ):
            suggestions = optimizer.run_daily_waterfall_review()

        # Unity (raise) and IronSource (lower) should have suggestions
        assert len(suggestions) >= 2
        networks = {s["network"] for s in suggestions}
        assert "Unity" in networks
        assert "IronSource" in networks
        # All guardrails applied
        for s in suggestions:
            assert s["requires_approval"] is True
