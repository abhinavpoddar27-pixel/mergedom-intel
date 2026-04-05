"""
Module 4 — Budget Allocation Optimizer.

Builds spend-response curves per network, computes marginal ROAS (or
inverse-CPI when revenue data is unavailable), and recommends budget
reallocations that maximise total portfolio efficiency.

Two modes:
  • ROAS-optimised — when Singular revenue data is available.
  • CPI-optimised  — fallback when revenue returns $0.  Maximises
    installs-per-dollar instead.

Guardrails:
  • Total budget stays the same.
  • No network below $100/day minimum viable spend.
  • No network increases more than 50 % in one day.
  • Minimum 14 days of data per network for curve fitting.
"""

from __future__ import annotations

import logging
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Optional

from app.models.schemas import BudgetRecommendation, CampaignMetric

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

MIN_DAYS_FOR_CURVE = 14
MIN_VIABLE_SPEND = 100.0       # $100/day
MAX_INCREASE_PCT = 0.50        # 50 % max increase per day
_EPSILON = 1e-9


# ── SpendCurve dataclass ────────────────────────────────────────────────────

@dataclass
class SpendCurve:
    """Log-linear spend-response model: efficiency = a + b * ln(spend).

    *efficiency* is ROAS when revenue data is available, otherwise
    installs-per-dollar (1/CPI).
    """
    network: str = ""
    slope: float = 0.0         # b  (negative = diminishing returns)
    intercept: float = 0.0     # a
    r_squared: float = 0.0
    data_points: int = 0
    mode: str = "cpi"          # "roas" or "cpi"
    inflection_spend: float = 0.0  # where marginal efficiency ≈ 0

    def predict(self, spend: float) -> float:
        """Predicted efficiency at a given spend level."""
        if spend <= 0:
            return 0.0
        return self.intercept + self.slope * math.log(spend)

    def marginal(self, spend: float) -> float:
        """Marginal efficiency = d(efficiency)/d(spend) = b / spend."""
        if spend <= 0:
            return 0.0
        return self.slope / spend


# ── BudgetOptimizer ──────────────────────────────────────────────────────────

class BudgetOptimizer:
    """Builds spend-response curves and recommends budget shifts."""

    # ── 1. Build curves ───────────────────────────────────────────────────

    @staticmethod
    def build_spend_response_curves(
        historical: list[CampaignMetric],
        days: int = 30,
    ) -> dict[str, SpendCurve]:
        """Fit a log-linear curve per network over the last *days*.

        Returns ``{network: SpendCurve}``.  Networks with fewer than
        ``MIN_DAYS_FOR_CURVE`` data points are excluded.
        """
        cutoff = date.today() - timedelta(days=days)

        # Aggregate daily spend and efficiency per network
        daily: dict[str, dict[str, dict]] = defaultdict(
            lambda: defaultdict(lambda: {
                "spend": 0.0, "installs": 0, "revenue_d7": 0.0,
            })
        )
        for row in historical:
            if row.date and row.date >= cutoff:
                day_key = row.date.isoformat()
                d = daily[row.network][day_key]
                d["spend"] += row.spend
                d["installs"] += row.installs
                d["revenue_d7"] += row.revenue_d7

        curves: dict[str, SpendCurve] = {}
        for network, day_map in daily.items():
            if len(day_map) < MIN_DAYS_FOR_CURVE:
                logger.debug(
                    "Skipping %s: only %d days (need %d)",
                    network, len(day_map), MIN_DAYS_FOR_CURVE,
                )
                continue

            # Decide mode: ROAS if revenue available, else CPI
            total_rev = sum(d["revenue_d7"] for d in day_map.values())
            mode = "roas" if total_rev > 0 else "cpi"

            xs: list[float] = []  # ln(spend)
            ys: list[float] = []  # efficiency

            for d in day_map.values():
                spend = d["spend"]
                if spend <= 0:
                    continue
                x = math.log(spend)

                if mode == "roas":
                    eff = d["revenue_d7"] / spend if spend > 0 else 0.0
                else:
                    # Installs per dollar (higher = better)
                    eff = d["installs"] / spend if spend > 0 else 0.0

                xs.append(x)
                ys.append(eff)

            if len(xs) < MIN_DAYS_FOR_CURVE:
                continue

            slope, intercept, r_sq = _linear_regression(xs, ys)

            # Inflection: where marginal efficiency → 0 is at large spend,
            # but practically where predicted efficiency < 0
            inflection = 0.0
            if slope < 0 and intercept > 0:
                # intercept + slope * ln(spend) = 0 → spend = exp(-intercept/slope)
                inflection = math.exp(-intercept / slope)

            curves[network] = SpendCurve(
                network=network,
                slope=slope,
                intercept=intercept,
                r_squared=r_sq,
                data_points=len(xs),
                mode=mode,
                inflection_spend=round(inflection, 2),
            )

        logger.info("Built spend curves for %d networks", len(curves))
        return curves

    # ── 2. Marginal ROAS ─────────────────────────────────────────────────

    @staticmethod
    def calculate_marginal_roas(
        network: str, current_spend: float, curve: SpendCurve
    ) -> float:
        """Marginal efficiency at current spend level."""
        return curve.marginal(current_spend)

    # ── 3. Optimise allocation ────────────────────────────────────────────

    @staticmethod
    def optimize_allocation(
        current_budgets: dict[str, float],
        total_budget: float,
        curves: dict[str, SpendCurve],
    ) -> list[BudgetRecommendation]:
        """Find allocation that maximises total portfolio efficiency.

        Uses iterative marginal-gain rebalancing:
          1. Start everyone at minimum viable spend.
          2. Allocate remaining budget in $50 increments to the network
             with the highest marginal efficiency at its current level.
          3. Apply guardrails (max +50 % increase, min viable spend).
        """
        networks = [n for n in current_budgets if n in curves]
        if not networks:
            return []

        # Initialise at minimum
        alloc: dict[str, float] = {n: MIN_VIABLE_SPEND for n in networks}
        remaining = total_budget - sum(alloc.values())

        if remaining < 0:
            # Budget too small for all networks at minimum
            # Distribute proportionally
            ratio = total_budget / sum(alloc.values()) if sum(alloc.values()) > 0 else 0
            alloc = {n: round(v * ratio, 2) for n, v in alloc.items()}
            remaining = 0

        # Greedy allocation in $50 increments
        increment = 50.0
        while remaining >= increment:
            best_net = None
            best_marginal = -float("inf")

            for n in networks:
                m = curves[n].marginal(alloc[n])
                if m > best_marginal:
                    best_marginal = m
                    best_net = n

            if best_net is None or best_marginal <= 0:
                # Distribute remainder evenly
                per_net = remaining / len(networks)
                for n in networks:
                    alloc[n] += per_net
                remaining = 0
                break

            alloc[best_net] += increment
            remaining -= increment

        # Distribute any leftover dust
        if remaining > 0 and networks:
            alloc[networks[0]] += remaining

        # Apply guardrails
        recommendations: list[BudgetRecommendation] = []
        for network in networks:
            current = current_budgets[network]
            recommended = alloc[network]

            # Cap increase at 50 %
            max_allowed = current * (1 + MAX_INCREASE_PCT)
            if recommended > max_allowed:
                recommended = max_allowed

            # Floor at minimum viable
            recommended = max(MIN_VIABLE_SPEND, recommended)
            recommended = round(recommended, 2)

            curve = curves[network]
            current_m = curve.marginal(current) if current > 0 else 0.0
            projected_m = curve.marginal(recommended)

            # Confidence based on curve fit quality + data depth
            confidence = _recommendation_confidence(curve, current, recommended)

            recommendations.append(BudgetRecommendation(
                network=network,
                current_spend=round(current, 2),
                recommended_spend=recommended,
                current_marginal_roas=round(current_m, 6),
                projected_marginal_roas=round(projected_m, 6),
                confidence=round(confidence, 1),
            ))

        # Sort by absolute change descending
        recommendations.sort(
            key=lambda r: abs(r.recommended_spend - r.current_spend),
            reverse=True,
        )
        return recommendations

    # ── 4. What-if analysis ───────────────────────────────────────────────

    @staticmethod
    def generate_what_if(
        scenario: dict[str, float],
        curves: dict[str, SpendCurve],
    ) -> dict[str, Any]:
        """Project impact of a budget scenario.

        *scenario*: ``{network: new_spend}``.
        Returns per-network projected efficiency and net portfolio change.
        """
        projections: dict[str, dict] = {}
        for network, new_spend in scenario.items():
            curve = curves.get(network)
            if not curve:
                continue
            projected_eff = curve.predict(new_spend)
            marginal = curve.marginal(new_spend)
            projections[network] = {
                "spend": new_spend,
                "projected_efficiency": round(projected_eff, 4),
                "marginal_efficiency": round(marginal, 6),
                "mode": curve.mode,
            }

        # Net portfolio efficiency
        total_spend = sum(p["spend"] for p in projections.values())
        if total_spend > 0:
            weighted_eff = sum(
                p["spend"] * p["projected_efficiency"]
                for p in projections.values()
            ) / total_spend
        else:
            weighted_eff = 0.0

        return {
            "networks": projections,
            "total_spend": round(total_spend, 2),
            "weighted_portfolio_efficiency": round(weighted_eff, 4),
        }

    # ── 5. Daily review pipeline ──────────────────────────────────────────

    def run_daily_budget_review(self) -> list[BudgetRecommendation]:
        """Full daily budget optimization pipeline."""
        # a. Historical data
        historical = self._load_historical()
        if not historical:
            logger.info("No historical data for budget optimization")
            return []

        # b. Build curves
        curves = self.build_spend_response_curves(historical, days=30)
        if not curves:
            logger.info("No curves built — insufficient data")
            return []

        # c/d. Current budgets + optimise
        current_budgets: dict[str, float] = {}
        for row in historical:
            if row.date == date.today() - timedelta(days=1):
                current_budgets[row.network] = (
                    current_budgets.get(row.network, 0) + row.spend
                )

        if not current_budgets:
            # Use latest available day
            latest_date = max(
                (r.date for r in historical if r.date), default=None
            )
            if latest_date:
                for row in historical:
                    if row.date == latest_date:
                        current_budgets[row.network] = (
                            current_budgets.get(row.network, 0) + row.spend
                        )

        total_budget = sum(current_budgets.values())
        recommendations = self.optimize_allocation(
            current_budgets, total_budget, curves
        )

        # e. Post significant recommendations
        self._deliver_recommendations(recommendations)

        # f. Track accuracy
        self.track_recommendation_accuracy(historical, curves)

        return recommendations

    # ── 6. Track accuracy ─────────────────────────────────────────────────

    @staticmethod
    def track_recommendation_accuracy(
        historical: list[CampaignMetric],
        curves: dict[str, SpendCurve],
    ) -> dict[str, float]:
        """Compare past curve predictions with actual outcomes.

        Returns ``{network: accuracy_score}`` where 1.0 = perfect.
        """
        accuracy: dict[str, float] = {}

        yesterday = date.today() - timedelta(days=1)
        for network, curve in curves.items():
            actuals = [
                r for r in historical
                if r.network == network and r.date == yesterday
            ]
            if not actuals:
                continue

            actual_spend = sum(r.spend for r in actuals)
            actual_installs = sum(r.installs for r in actuals)
            if actual_spend <= 0:
                continue

            actual_eff = actual_installs / actual_spend  # CPI mode
            predicted_eff = curve.predict(actual_spend)

            if predicted_eff <= 0:
                accuracy[network] = 0.0
                continue

            error = abs(predicted_eff - actual_eff) / predicted_eff
            accuracy[network] = round(max(0.0, 1.0 - error), 3)

        if accuracy:
            avg = sum(accuracy.values()) / len(accuracy)
            logger.info(
                "Budget curve accuracy: %s (avg %.2f)",
                accuracy, avg,
            )

        return accuracy

    # ── Internal helpers ──────────────────────────────────────────────────

    @staticmethod
    def _load_historical() -> list[CampaignMetric]:
        try:
            from app.services.airtable import AirtableService
            return AirtableService().get_recent_metrics(days=30)
        except Exception as exc:
            logger.warning("Historical data load failed: %s", exc)
            return []

    @staticmethod
    def _deliver_recommendations(recs: list[BudgetRecommendation]) -> None:
        significant = [
            r for r in recs
            if abs(r.recommended_spend - r.current_spend) > r.current_spend * 0.10
        ]
        if not significant:
            return

        try:
            from app.services.claude import ClaudeService
            claude = ClaudeService()
            spend_data = {
                r.network: {
                    "current": r.current_spend,
                    "recommended": r.recommended_spend,
                    "marginal_roas": r.current_marginal_roas,
                }
                for r in significant
            }
            claude_recs = claude.suggest_budget_allocation(spend_data)
            logger.info("Claude budget suggestions: %d", len(claude_recs))
        except Exception as exc:
            logger.warning("Claude budget suggestion failed: %s", exc)

        # Draft email for stakeholder approval if large shifts
        large = [r for r in significant if abs(r.recommended_spend - r.current_spend) > 500]
        if large:
            try:
                from app.services.gmail import GmailService
                from app.config import get_settings
                cfg = get_settings()
                gmail = GmailService()

                body = ["<h2>Budget Reallocation Proposal</h2>"]
                body.append("<table border='1' cellpadding='4' style='border-collapse:collapse;'>")
                body.append(
                    "<tr><th>Network</th><th>Current</th><th>Recommended</th>"
                    "<th>Change</th><th>Confidence</th></tr>"
                )
                for r in large:
                    delta = r.recommended_spend - r.current_spend
                    body.append(
                        f"<tr><td>{r.network}</td>"
                        f"<td>${r.current_spend:,.0f}</td>"
                        f"<td>${r.recommended_spend:,.0f}</td>"
                        f"<td>${delta:+,.0f}</td>"
                        f"<td>{r.confidence:.0f}%</td></tr>"
                    )
                body.append("</table>")
                gmail.create_draft(
                    cfg.natasha_email,
                    f"Budget Proposal — {date.today().isoformat()}",
                    "\n".join(body),
                )
            except Exception as exc:
                logger.warning("Budget email draft failed: %s", exc)


# ── Private helpers ──────────────────────────────────────────────────────────

def _linear_regression(
    xs: list[float], ys: list[float]
) -> tuple[float, float, float]:
    """Simple ordinary least squares.  Returns (slope, intercept, r²)."""
    n = len(xs)
    if n < 2:
        return (0.0, 0.0, 0.0)

    mean_x = statistics.mean(xs)
    mean_y = statistics.mean(ys)

    ss_xy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    ss_xx = sum((x - mean_x) ** 2 for x in xs)
    ss_yy = sum((y - mean_y) ** 2 for y in ys)

    if ss_xx == 0:
        return (0.0, mean_y, 0.0)

    slope = ss_xy / ss_xx
    intercept = mean_y - slope * mean_x

    # R² (coefficient of determination)
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    r_sq = 1 - (ss_res / ss_yy) if ss_yy > 0 else 0.0

    return (slope, intercept, max(0.0, min(1.0, r_sq)))


def _recommendation_confidence(
    curve: SpendCurve, current: float, recommended: float
) -> float:
    """Score 0-100 based on curve quality and recommendation magnitude."""
    # Base from R²
    base = curve.r_squared * 50  # 0-50

    # Data depth bonus (14 days = 0, 30 days = 20)
    depth_bonus = min(20.0, (curve.data_points - MIN_DAYS_FOR_CURVE) / 16 * 20)

    # Penalty for large shifts (higher uncertainty)
    if current > 0:
        shift_pct = abs(recommended - current) / current
        shift_penalty = min(20.0, shift_pct * 40)
    else:
        shift_penalty = 10.0

    # Small bonus for more data
    return min(100.0, max(0.0, base + depth_bonus + 10 - shift_penalty))
