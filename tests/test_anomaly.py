"""Tests for the anomaly detection engine."""

from datetime import date, timedelta
from unittest.mock import patch

import pytest

from app.models.schemas import (
    Alert,
    AlertType,
    CampaignMetric,
    CreativeMetric,
    Severity,
)
from app.pipelines.anomaly import AnomalyDetector


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_campaign_row(
    day_offset: int,
    network: str = "meta",
    campaign_id: str = "c1",
    campaign_name: str = "Test Campaign",
    spend: float = 100.0,
    installs: int = 50,
    cpi: float = 2.0,
    ipm: float = 3.0,
    ctr: float = 2.5,
    roas_d7: float = 1.2,
) -> CampaignMetric:
    """Build a CampaignMetric with a date *day_offset* days from today."""
    return CampaignMetric(
        date=date.today() - timedelta(days=day_offset),
        network=network,
        campaign_id=campaign_id,
        campaign_name=campaign_name,
        os="ios",
        country="US",
        spend=spend,
        installs=installs,
        impressions=10_000,
        clicks=500,
        cpi=cpi,
        ipm=ipm,
        ctr=ctr,
        roas_d1=0.5,
        roas_d7=roas_d7,
        roas_d30=2.0,
        revenue_d1=50.0,
        revenue_d7=120.0,
        revenue_d30=200.0,
    )


def _make_creative_row(
    creative_id: str = "cr1",
    ipm: float = 3.0,
    ctr: float = 2.5,
    fatigue_score: float = 0.2,
) -> CreativeMetric:
    return CreativeMetric(
        creative_id=creative_id,
        concept_tag="UGC_v1",
        platform="meta",
        ipm=ipm,
        ctr=ctr,
        roas_d7=1.5,
        spend=200.0,
        impressions=80_000,
        installs=180,
        fatigue_score=fatigue_score,
        days_live=14,
        fatigue_curve_slope=-0.01,
    )


def _build_stable_history(
    days: int = 10, **overrides
) -> list[CampaignMetric]:
    """Generate *days* of stable campaign data."""
    return [_make_campaign_row(day_offset=i, **overrides) for i in range(days, 0, -1)]


# ── Tests: rolling stats ────────────────────────────────────────────────────

class TestRollingStats:
    def test_basic_stats(self):
        detector = AnomalyDetector()
        values = [10.0, 12.0, 11.0, 10.5, 11.5, 12.5, 10.0]
        avg, std = detector._calculate_rolling_stats(values, window=7)
        assert abs(avg - 11.071) < 0.1
        assert std > 0

    def test_fewer_than_window(self):
        detector = AnomalyDetector()
        avg, std = detector._calculate_rolling_stats([5.0, 6.0, 7.0], window=7)
        assert abs(avg - 6.0) < 0.01
        assert std > 0

    def test_single_value_returns_zero(self):
        detector = AnomalyDetector()
        avg, std = detector._calculate_rolling_stats([42.0])
        assert avg == 0.0
        assert std == 0.0

    def test_empty_returns_zero(self):
        detector = AnomalyDetector()
        avg, std = detector._calculate_rolling_stats([])
        assert avg == 0.0
        assert std == 0.0

    def test_constant_values_zero_stdev(self):
        detector = AnomalyDetector()
        avg, std = detector._calculate_rolling_stats([5.0] * 7)
        assert abs(avg - 5.0) < 0.01
        assert std == 0.0


# ── Tests: normal data (no alerts) ──────────────────────────────────────────

class TestNormalData:
    def test_stable_data_produces_no_alerts(self):
        """10 days of identical data → current matching history → no alerts."""
        detector = AnomalyDetector()
        history = _build_stable_history(days=10)
        current = [_make_campaign_row(day_offset=0)]

        alerts = detector.detect_campaign_anomalies(current, history, {})
        assert alerts == []

    def test_small_fluctuation_no_alerts(self):
        """A 10% spend bump on stable data should not trigger."""
        detector = AnomalyDetector()
        history = _build_stable_history(days=10, spend=100.0)
        # 10% increase is well within 1.5 std of constant data (std=0)
        # …but std=0 means division returns None → no alert. Correct.
        current = [_make_campaign_row(day_offset=0, spend=110.0)]

        alerts = detector.detect_campaign_anomalies(current, history, {})
        assert alerts == []


# ── Tests: spike detection ───────────────────────────────────────────────────

class TestSpikeDetection:
    def _varied_history(self, field: str, base: float, noise: float):
        """Build 10 days with slight variation so stdev > 0."""
        rows = []
        for i in range(10, 0, -1):
            val = base + (noise if i % 2 == 0 else -noise)
            rows.append(_make_campaign_row(day_offset=i, **{field: val}))
        return rows

    def test_spend_spike_detected(self):
        detector = AnomalyDetector()
        history = self._varied_history("spend", base=100.0, noise=5.0)
        # 4× the std above mean → should be CRITICAL
        current = [_make_campaign_row(day_offset=0, spend=200.0)]

        alerts = detector.detect_campaign_anomalies(current, history, {})
        assert len(alerts) >= 1
        spend_alerts = [a for a in alerts if "spend" in a.title.lower()]
        assert len(spend_alerts) >= 1
        assert spend_alerts[0].severity in (Severity.critical, Severity.high)

    def test_cpi_spike_detected(self):
        detector = AnomalyDetector()
        history = self._varied_history("cpi", base=2.0, noise=0.2)
        current = [_make_campaign_row(day_offset=0, cpi=8.0)]

        alerts = detector.detect_campaign_anomalies(current, history, {})
        cpi_alerts = [a for a in alerts if "cpi" in a.title.lower()]
        assert len(cpi_alerts) >= 1

    def test_spend_drop_detected(self):
        detector = AnomalyDetector()
        history = self._varied_history("spend", base=100.0, noise=5.0)
        current = [_make_campaign_row(day_offset=0, spend=10.0)]

        alerts = detector.detect_campaign_anomalies(current, history, {})
        drop_alerts = [a for a in alerts if "spend" in a.title.lower()]
        assert len(drop_alerts) >= 1


# ── Tests: consecutive decline ───────────────────────────────────────────────

class TestConsecutiveDecline:
    def test_roas_declining_3_days(self):
        """3-day monotonic ROAS decline → flagged."""
        detector = AnomalyDetector()
        # Need enough stable history so the z-score alert doesn't fire
        # at a higher severity and dedup the consecutive alert away.
        # Use 10 days of stable ROAS, then a 3-day decline tail.
        history = _build_stable_history(days=7, roas_d7=1.5)
        # Append the declining tail
        history.append(_make_campaign_row(day_offset=2, roas_d7=1.4))
        history.append(_make_campaign_row(day_offset=1, roas_d7=1.3))
        current = [_make_campaign_row(day_offset=0, roas_d7=1.2)]

        alerts = detector.detect_campaign_anomalies(current, history, {})
        consec = [a for a in alerts if "consecutive" in a.title.lower()]
        assert len(consec) >= 1
        assert consec[0].severity in (Severity.high, Severity.medium)

    def test_no_decline_when_values_flat(self):
        """Flat ROAS → no consecutive decline alert."""
        detector = AnomalyDetector()
        history = _build_stable_history(days=5, roas_d7=1.2)
        current = [_make_campaign_row(day_offset=0, roas_d7=1.2)]

        alerts = detector.detect_campaign_anomalies(current, history, {})
        consec = [a for a in alerts if "consecutive" in a.title.lower()]
        assert consec == []


# ── Tests: weekend suppression ───────────────────────────────────────────────

class TestWeekendSuppression:
    def test_weekend_spend_drop_suppressed(self):
        """Spend drop on Saturday should be suppressed."""
        detector = AnomalyDetector()
        history = []
        for i in range(10, 0, -1):
            history.append(
                _make_campaign_row(
                    day_offset=i,
                    spend=100.0 + (5.0 if i % 2 == 0 else -5.0),
                )
            )
        current = [_make_campaign_row(day_offset=0, spend=10.0)]

        # Patch datetime to a Saturday
        from datetime import datetime as real_dt

        class FakeSaturday(real_dt):
            @classmethod
            def now(cls, tz=None):
                # Return a Saturday (2025-06-07 is a Saturday)
                return real_dt(2025, 6, 7, 10, 0, 0, tzinfo=tz)

        with patch("app.pipelines.anomaly.datetime", FakeSaturday):
            alerts = detector.detect_campaign_anomalies(current, history, {})
            spend_alerts = [
                a for a in alerts
                if "spend" in a.title.lower() and "drop" in a.title.lower()
            ]
            # Weekend spend drops should be suppressed
            assert spend_alerts == []

    def test_weekday_spend_drop_not_suppressed(self):
        """Spend drop on a Tuesday should NOT be suppressed."""
        detector = AnomalyDetector()
        history = []
        for i in range(10, 0, -1):
            history.append(
                _make_campaign_row(
                    day_offset=i,
                    spend=100.0 + (5.0 if i % 2 == 0 else -5.0),
                )
            )
        current = [_make_campaign_row(day_offset=0, spend=10.0)]

        from datetime import datetime as real_dt

        class FakeTuesday(real_dt):
            @classmethod
            def now(cls, tz=None):
                # 2025-06-03 is a Tuesday
                return real_dt(2025, 6, 3, 10, 0, 0, tzinfo=tz)

        with patch("app.pipelines.anomaly.datetime", FakeTuesday):
            alerts = detector.detect_campaign_anomalies(current, history, {})
            spend_alerts = [
                a for a in alerts
                if "spend" in a.title.lower()
            ]
            assert len(spend_alerts) >= 1


# ── Tests: known patterns ────────────────────────────────────────────────────

class TestKnownPatterns:
    def test_google_medium_spend_swing_suppressed(self):
        """Medium-severity Google Ads spend alert should be suppressed."""
        detector = AnomalyDetector()
        alert = Alert(
            alert_id="test",
            alert_type=AlertType.anomaly,
            severity=Severity.medium,
            title="SPEND ↑ 25% on google adwords",
            body="Google AdWords campaign: spend is 125 vs 100",
            related_campaign="g1",
            confidence=70.0,
        )
        assert detector._is_known_pattern(alert) is True

    def test_google_high_spend_not_suppressed(self):
        """High-severity Google Ads spend alert should NOT be suppressed."""
        detector = AnomalyDetector()
        alert = Alert(
            alert_id="test",
            alert_type=AlertType.anomaly,
            severity=Severity.high,
            title="SPEND ↑ 80% on google adwords",
            body="Google AdWords campaign: spend is 180 vs 100",
            related_campaign="g1",
            confidence=85.0,
        )
        assert detector._is_known_pattern(alert) is False

    def test_volatile_network_wider_thresholds(self):
        """AdAction Interactive gets 2× wider thresholds → fewer alerts.

        With normal thresholds (1.5σ), a ~2σ spike would trigger MEDIUM.
        With 2× multiplier the effective threshold is 3.0σ, so a ~2σ spike
        is within bounds and should be suppressed.
        """
        detector = AnomalyDetector()

        # Build history with some noise (stdev ≈ 8.4)
        history = []
        for i in range(10, 0, -1):
            history.append(
                _make_campaign_row(
                    day_offset=i,
                    network="AdAction Interactive",
                    spend=100.0 + (8.0 if i % 2 == 0 else -8.0),
                )
            )

        # ~2σ spike (100 + 2*8.4 ≈ 117) — under the 3σ effective threshold
        current = [
            _make_campaign_row(
                day_offset=0,
                network="AdAction Interactive",
                spend=117.0,
            )
        ]
        alerts = detector.detect_campaign_anomalies(current, history, {})
        spend_alerts = [a for a in alerts if "spend" in a.title.lower()]
        assert spend_alerts == []


# ── Tests: edge cases ────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_insufficient_history(self):
        """Fewer than 3 historical rows → no alerts (not enough data)."""
        detector = AnomalyDetector()
        history = [_make_campaign_row(day_offset=2)]
        current = [_make_campaign_row(day_offset=0, spend=500.0)]

        alerts = detector.detect_campaign_anomalies(current, history, {})
        assert alerts == []

    def test_empty_current(self):
        """No current data → no alerts."""
        detector = AnomalyDetector()
        history = _build_stable_history(days=10)
        alerts = detector.detect_campaign_anomalies([], history, {})
        assert alerts == []

    def test_empty_history(self):
        """No historical data → no alerts."""
        detector = AnomalyDetector()
        current = [_make_campaign_row(day_offset=0)]
        alerts = detector.detect_campaign_anomalies(current, [], {})
        assert alerts == []

    def test_different_campaigns_independent(self):
        """Anomalies in campaign A should not affect campaign B."""
        detector = AnomalyDetector()

        hist_a = []
        hist_b = []
        for i in range(10, 0, -1):
            hist_a.append(
                _make_campaign_row(
                    day_offset=i, campaign_id="A",
                    spend=100.0 + (5.0 if i % 2 == 0 else -5.0),
                )
            )
            hist_b.append(
                _make_campaign_row(
                    day_offset=i, campaign_id="B",
                    spend=50.0 + (3.0 if i % 2 == 0 else -3.0),
                )
            )

        current = [
            _make_campaign_row(day_offset=0, campaign_id="A", spend=500.0),
            _make_campaign_row(day_offset=0, campaign_id="B", spend=50.0),
        ]
        alerts = detector.detect_campaign_anomalies(
            current, hist_a + hist_b, {}
        )
        # Only campaign A should have alerts
        for alert in alerts:
            assert alert.related_campaign == "A"


# ── Tests: deduplication & ranking ───────────────────────────────────────────

class TestDeduplicationAndRanking:
    def test_dedup_keeps_highest_severity(self):
        detector = AnomalyDetector()
        # Both are z-score alerts (no "consecutive" in title) → same signal
        alerts = [
            Alert(
                alert_id="1", alert_type=AlertType.anomaly,
                severity=Severity.medium, title="SPEND ↑ 20% on meta",
                body="", related_campaign="c1", confidence=60.0,
            ),
            Alert(
                alert_id="2", alert_type=AlertType.anomaly,
                severity=Severity.high, title="SPEND ↑ 50% on meta",
                body="", related_campaign="c1", confidence=80.0,
            ),
        ]
        result = detector._deduplicate_alerts(alerts)
        assert len(result) == 1
        assert result[0].severity == Severity.high

    def test_dedup_preserves_consecutive_vs_zscore(self):
        """Z-score and consecutive alerts for same campaign are kept separate."""
        detector = AnomalyDetector()
        alerts = [
            Alert(
                alert_id="1", alert_type=AlertType.anomaly,
                severity=Severity.critical, title="ROAS_D7 ↓ 40% on meta",
                body="", related_campaign="c1", confidence=90.0,
            ),
            Alert(
                alert_id="2", alert_type=AlertType.anomaly,
                severity=Severity.medium,
                title="ROAS_D7 declining 3 consecutive days on meta",
                body="", related_campaign="c1", confidence=74.0,
            ),
        ]
        result = detector._deduplicate_alerts(alerts)
        assert len(result) == 2

    def test_ranking_critical_first(self):
        detector = AnomalyDetector()
        alerts = [
            Alert(
                alert_id="1", alert_type=AlertType.anomaly,
                severity=Severity.medium, title="a",
                body="", related_campaign="c1", confidence=90.0,
            ),
            Alert(
                alert_id="2", alert_type=AlertType.budget,
                severity=Severity.critical, title="b",
                body="", related_campaign="c2", confidence=70.0,
            ),
            Alert(
                alert_id="3", alert_type=AlertType.creative_fatigue,
                severity=Severity.high, title="c",
                body="", related_campaign="c3", confidence=80.0,
            ),
        ]
        ranked = detector.rank_alerts(alerts)
        assert ranked[0].severity == Severity.critical
        assert ranked[1].severity == Severity.high
        assert ranked[2].severity == Severity.medium


# ── Tests: learned weights ───────────────────────────────────────────────────

class TestLearnedWeights:
    def test_weight_boosts_confidence(self):
        detector = AnomalyDetector()
        alerts = [
            Alert(
                alert_id="1", alert_type=AlertType.anomaly,
                severity=Severity.high, title="test",
                body="", related_campaign="c1", confidence=70.0,
            ),
        ]
        weights = {"anomaly": 1.3}
        result = detector._apply_learned_weights(alerts, weights)
        assert result[0].confidence == 91.0  # 70 * 1.3

    def test_weight_dampens_confidence(self):
        detector = AnomalyDetector()
        alerts = [
            Alert(
                alert_id="1", alert_type=AlertType.anomaly,
                severity=Severity.high, title="test",
                body="", related_campaign="c1", confidence=70.0,
            ),
        ]
        weights = {"anomaly": 0.5}
        result = detector._apply_learned_weights(alerts, weights)
        assert result[0].confidence == 35.0  # 70 * 0.5

    def test_confidence_capped_at_100(self):
        detector = AnomalyDetector()
        alerts = [
            Alert(
                alert_id="1", alert_type=AlertType.anomaly,
                severity=Severity.high, title="test",
                body="", related_campaign="c1", confidence=90.0,
            ),
        ]
        weights = {"anomaly": 2.0}
        result = detector._apply_learned_weights(alerts, weights)
        assert result[0].confidence == 100.0


# ── Tests: creative anomalies ────────────────────────────────────────────────

class TestCreativeAnomalies:
    def test_creative_ipm_drop(self):
        detector = AnomalyDetector()
        # Stable history with variation
        history = []
        for _ in range(10):
            history.append(
                _make_creative_row(
                    ipm=3.0 + (0.3 if _ % 2 == 0 else -0.3),
                )
            )

        # Sharp IPM drop
        current = [_make_creative_row(ipm=0.5)]
        alerts = detector.detect_creative_anomalies(current, history)
        assert len(alerts) >= 1
        assert alerts[0].alert_type == AlertType.creative_fatigue

    def test_stable_creative_no_alerts(self):
        detector = AnomalyDetector()
        history = [_make_creative_row() for _ in range(10)]
        current = [_make_creative_row()]
        alerts = detector.detect_creative_anomalies(current, history)
        assert alerts == []
