"""Tests for the budget allocation optimizer."""

import math
from datetime import date, timedelta

import pytest

from app.models.schemas import BudgetRecommendation, CampaignMetric
from app.modules.budget import (
    BudgetOptimizer,
    SpendCurve,
    _linear_regression,
    _recommendation_confidence,
    MIN_VIABLE_SPEND,
    MAX_INCREASE_PCT,
    MIN_DAYS_FOR_CURVE,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _row(network: str, day_offset: int, spend: float, installs: int,
         revenue_d7: float = 0.0) -> CampaignMetric:
    return CampaignMetric(
        date=date.today() - timedelta(days=day_offset),
        network=network,
        campaign_id=f"c_{network}",
        campaign_name=f"Campaign {network}",
        os="ios", country="US",
        spend=spend, installs=installs,
        impressions=10000, clicks=500,
        cpi=spend / installs if installs > 0 else 0,
        ipm=5.0, ctr=5.0,
        roas_d7=revenue_d7 / spend if spend > 0 and revenue_d7 > 0 else 0,
        revenue_d7=revenue_d7,
    )


def _30_day_history(network: str, base_spend: float, base_installs: int,
                    revenue: float = 0.0) -> list[CampaignMetric]:
    """Generate 30 days of data with slight variation."""
    rows = []
    for d in range(30, 0, -1):
        factor = 1.0 + (d % 5 - 2) * 0.05  # ±10% variation
        rows.append(_row(
            network, d,
            spend=round(base_spend * factor, 2),
            installs=round(base_installs * factor),
            revenue_d7=round(revenue * factor, 2),
        ))
    return rows


# ── Linear regression ────────────────────────────────────────────────────────

class TestLinearRegression:
    def test_perfect_fit(self):
        xs = [1.0, 2.0, 3.0, 4.0]
        ys = [2.0, 4.0, 6.0, 8.0]  # y = 2x
        slope, intercept, r_sq = _linear_regression(xs, ys)
        assert abs(slope - 2.0) < 0.01
        assert abs(intercept) < 0.01
        assert abs(r_sq - 1.0) < 0.01

    def test_negative_slope(self):
        xs = [1.0, 2.0, 3.0, 4.0]
        ys = [8.0, 6.0, 4.0, 2.0]  # y = -2x + 10
        slope, intercept, r_sq = _linear_regression(xs, ys)
        assert slope < 0
        assert r_sq > 0.99

    def test_single_point(self):
        slope, intercept, r_sq = _linear_regression([1.0], [5.0])
        assert slope == 0.0
        assert r_sq == 0.0

    def test_constant_x(self):
        slope, intercept, r_sq = _linear_regression([5.0, 5.0, 5.0], [1.0, 2.0, 3.0])
        assert slope == 0.0


# ── SpendCurve ───────────────────────────────────────────────────────────────

class TestSpendCurve:
    def test_predict(self):
        curve = SpendCurve(slope=-0.1, intercept=2.0)
        # At spend=1: 2.0 + -0.1 * ln(1) = 2.0 + 0 = 2.0
        assert abs(curve.predict(1.0) - 2.0) < 0.01
        # At spend=e: 2.0 + -0.1 * 1 = 1.9
        assert abs(curve.predict(math.e) - 1.9) < 0.01

    def test_predict_zero_spend(self):
        curve = SpendCurve(slope=-0.1, intercept=2.0)
        assert curve.predict(0) == 0.0

    def test_marginal(self):
        curve = SpendCurve(slope=-0.1, intercept=2.0)
        # At spend=100: -0.1/100 = -0.001
        assert abs(curve.marginal(100) - (-0.001)) < 0.0001

    def test_marginal_zero_spend(self):
        curve = SpendCurve(slope=-0.1, intercept=2.0)
        assert curve.marginal(0) == 0.0


# ── Build curves ─────────────────────────────────────────────────────────────

class TestBuildCurves:
    def test_builds_cpi_mode_when_no_revenue(self):
        optimizer = BudgetOptimizer()
        historical = _30_day_history("applovin", base_spend=3800, base_installs=4040)
        curves = optimizer.build_spend_response_curves(historical)
        assert "applovin" in curves
        assert curves["applovin"].mode == "cpi"
        assert curves["applovin"].data_points >= MIN_DAYS_FOR_CURVE

    def test_builds_roas_mode_when_revenue_available(self):
        optimizer = BudgetOptimizer()
        historical = _30_day_history(
            "applovin", base_spend=3800, base_installs=4040, revenue=5000
        )
        curves = optimizer.build_spend_response_curves(historical)
        assert curves["applovin"].mode == "roas"

    def test_skips_network_with_insufficient_data(self):
        optimizer = BudgetOptimizer()
        # Only 5 days of data
        historical = [
            _row("small_network", d, 100, 50) for d in range(5, 0, -1)
        ]
        curves = optimizer.build_spend_response_curves(historical)
        assert "small_network" not in curves

    def test_multiple_networks(self):
        optimizer = BudgetOptimizer()
        historical = (
            _30_day_history("applovin", 3800, 4040) +
            _30_day_history("google", 260, 243) +
            _30_day_history("facebook", 380, 27)
        )
        curves = optimizer.build_spend_response_curves(historical)
        assert len(curves) == 3

    def test_curve_has_r_squared(self):
        optimizer = BudgetOptimizer()
        historical = _30_day_history("applovin", 3800, 4040)
        curves = optimizer.build_spend_response_curves(historical)
        assert 0 <= curves["applovin"].r_squared <= 1


# ── Optimize allocation ──────────────────────────────────────────────────────

class TestOptimizeAllocation:
    def _curves(self) -> dict[str, SpendCurve]:
        """Mock curves: applovin efficient, facebook inefficient.

        Marginal efficiency = slope / spend.  A more-negative slope
        with a lower intercept makes facebook clearly the worst choice
        at every spend level.
        """
        return {
            "applovin": SpendCurve(
                network="applovin", slope=0.0002, intercept=0.0005,
                r_squared=0.8, data_points=30, mode="cpi",
            ),
            "google": SpendCurve(
                network="google", slope=0.00015, intercept=0.0003,
                r_squared=0.75, data_points=28, mode="cpi",
            ),
            "facebook": SpendCurve(
                network="facebook", slope=-0.0005, intercept=0.001,
                r_squared=0.6, data_points=25, mode="cpi",
            ),
        }

    def test_returns_budget_recommendations(self):
        optimizer = BudgetOptimizer()
        budgets = {"applovin": 3800, "google": 260, "facebook": 380}
        recs = optimizer.optimize_allocation(budgets, 4440, self._curves())
        assert all(isinstance(r, BudgetRecommendation) for r in recs)
        assert len(recs) == 3

    def test_total_budget_preserved(self):
        """Total recommended spend should not exceed original total."""
        optimizer = BudgetOptimizer()
        budgets = {"applovin": 3800, "google": 260, "facebook": 380}
        total = sum(budgets.values())
        recs = optimizer.optimize_allocation(budgets, total, self._curves())
        rec_total = sum(r.recommended_spend for r in recs)
        # Allow small float rounding
        assert abs(rec_total - total) < total * 0.01 or rec_total <= total * 1.01

    def test_shifts_away_from_high_cpi(self):
        """Should recommend lower spend on facebook (high CPI ~$14)."""
        optimizer = BudgetOptimizer()
        budgets = {"applovin": 3800, "google": 260, "facebook": 380}
        recs = optimizer.optimize_allocation(budgets, 4440, self._curves())
        fb = next(r for r in recs if r.network == "facebook")
        # Facebook should not increase (it's inefficient)
        assert fb.recommended_spend <= fb.current_spend * 1.1

    def test_minimum_viable_spend_enforced(self):
        optimizer = BudgetOptimizer()
        budgets = {"applovin": 3800, "google": 50, "facebook": 50}
        recs = optimizer.optimize_allocation(budgets, 3900, self._curves())
        for r in recs:
            assert r.recommended_spend >= MIN_VIABLE_SPEND

    def test_max_increase_capped_at_50_pct(self):
        optimizer = BudgetOptimizer()
        budgets = {"applovin": 200, "google": 200, "facebook": 200}
        recs = optimizer.optimize_allocation(budgets, 600, self._curves())
        for r in recs:
            max_allowed = r.current_spend * (1 + MAX_INCREASE_PCT)
            assert r.recommended_spend <= max_allowed + 1.0  # float tolerance

    def test_no_curves_returns_empty(self):
        optimizer = BudgetOptimizer()
        recs = optimizer.optimize_allocation({"x": 100}, 100, {})
        assert recs == []

    def test_confidence_included(self):
        optimizer = BudgetOptimizer()
        budgets = {"applovin": 3800, "google": 260}
        curves = {k: v for k, v in self._curves().items() if k in budgets}
        recs = optimizer.optimize_allocation(budgets, 4060, curves)
        for r in recs:
            assert 0 <= r.confidence <= 100


# ── What-if analysis ─────────────────────────────────────────────────────────

class TestWhatIf:
    def test_returns_projections(self):
        optimizer = BudgetOptimizer()
        curves = {
            "applovin": SpendCurve(slope=-0.0001, intercept=0.003, mode="cpi"),
            "google": SpendCurve(slope=-0.0001, intercept=0.004, mode="cpi"),
        }
        result = optimizer.generate_what_if(
            {"applovin": 4500, "google": 500}, curves
        )
        assert "networks" in result
        assert "total_spend" in result
        assert result["total_spend"] == 5000
        assert "weighted_portfolio_efficiency" in result

    def test_missing_curve_ignored(self):
        optimizer = BudgetOptimizer()
        result = optimizer.generate_what_if(
            {"applovin": 4500, "unknown": 200},
            {"applovin": SpendCurve(slope=-0.0001, intercept=0.003)},
        )
        assert "unknown" not in result["networks"]


# ── Accuracy tracking ────────────────────────────────────────────────────────

class TestAccuracy:
    def test_perfect_prediction(self):
        optimizer = BudgetOptimizer()
        # Curve predicts exactly what happened
        curve = SpendCurve(slope=0.0, intercept=1.0, mode="cpi")
        # intercept=1.0 predicts 1.0 installs/$, actual = 100/100 = 1.0
        historical = [_row("net", 1, 100, 100)]
        accuracy = optimizer.track_recommendation_accuracy(
            historical, {"net": curve}
        )
        assert "net" in accuracy
        assert accuracy["net"] > 0.9

    def test_no_data_returns_empty(self):
        optimizer = BudgetOptimizer()
        accuracy = optimizer.track_recommendation_accuracy([], {})
        assert accuracy == {}


# ── Confidence scoring ───────────────────────────────────────────────────────

class TestConfidence:
    def test_high_r_squared_high_confidence(self):
        curve = SpendCurve(r_squared=0.95, data_points=30)
        conf = _recommendation_confidence(curve, 1000, 1050)
        assert conf > 50

    def test_low_r_squared_low_confidence(self):
        curve = SpendCurve(r_squared=0.1, data_points=14)
        conf = _recommendation_confidence(curve, 1000, 1050)
        assert conf < 30

    def test_large_shift_penalised(self):
        curve = SpendCurve(r_squared=0.8, data_points=25)
        small_shift = _recommendation_confidence(curve, 1000, 1050)
        large_shift = _recommendation_confidence(curve, 1000, 2000)
        assert small_shift > large_shift
