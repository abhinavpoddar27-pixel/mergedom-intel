"""
Module 3 — Waterfall Optimization.

Analyses MAX mediation waterfall data to identify sub-optimal floor prices
and recommends eCPM-based adjustments with revenue impact estimates.

Rules:
  eCPM > floor × 1.3             → raise floor
  eCPM < floor × 0.9             → lower floor
  fill_rate < 60 % + eCPM ok     → floor too high
  fill_rate > 95 %               → floor possibly too low

Guardrails:
  Max ±15 % floor change per day.
  Never below per-network minimums.
  Phase 1: manual approval required for all changes.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Optional

from app.models.schemas import WaterfallInstance

logger = logging.getLogger(__name__)

# ── Thresholds ───────────────────────────────────────────────────────────────

ECPM_RAISE_RATIO = 1.3     # eCPM > floor * 1.3 → raise
ECPM_LOWER_RATIO = 0.9     # eCPM < floor * 0.9 → lower
FILL_RATE_LOW = 0.60       # 60 %
FILL_RATE_HIGH = 0.95      # 95 %
MAX_FLOOR_CHANGE_PCT = 0.15  # ±15 % per day

# Minimum floor prices per network (USD)
NETWORK_FLOOR_MINIMUMS: dict[str, float] = {
    "IronSource": 0.50,
    "Unity": 0.50,
    "AppLovin": 1.00,
    "TapJoy": 0.25,
    "AdJoy": 0.10,
    "FreeCash": 0.10,
}

DEFAULT_MIN_FLOOR = 0.10


class WaterfallOptimizer:
    """Analyses waterfall data and generates floor-price suggestions."""

    # ── 1. Analyse waterfall ──────────────────────────────────────────────

    @staticmethod
    def analyze_waterfall(data: list[WaterfallInstance]) -> dict[str, Any]:
        """Evaluate every instance and tag it with an action signal.

        Returns::

            {
                "instances": [
                    {WaterfallInstance fields + "signal": str, "reason": str},
                    ...
                ],
                "summary": {
                    "total": int,
                    "raise": int,
                    "lower": int,
                    "ok": int,
                },
            }
        """
        results: list[dict] = []
        counts = {"raise": 0, "lower": 0, "ok": 0}

        for inst in data:
            signal, reason = _evaluate_instance(inst)
            results.append({
                "network": inst.network,
                "geo_tier": inst.geo_tier,
                "ad_format": inst.ad_format,
                "time_block": inst.time_block,
                "floor_price": inst.floor_price,
                "ecpm": inst.ecpm,
                "fill_rate": inst.fill_rate,
                "show_rate": inst.show_rate,
                "revenue": inst.revenue,
                "signal": signal,
                "reason": reason,
            })
            if signal in counts:
                counts[signal] += 1

        return {
            "instances": results,
            "summary": {
                "total": len(results),
                **counts,
            },
        }

    # ── 2. Generate suggestions ───────────────────────────────────────────

    @staticmethod
    def generate_suggestions(
        analysis: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Convert analysis signals into concrete floor-price suggestions."""
        suggestions: list[dict] = []

        for inst in analysis.get("instances", []):
            signal = inst["signal"]
            if signal == "ok":
                continue

            floor = inst["floor_price"]
            ecpm = inst["ecpm"]

            if signal == "raise":
                # Target: midpoint between current floor and eCPM
                target = (floor + ecpm) / 2
                recommended = round(target, 2)
            elif signal == "lower":
                # Target: slightly below eCPM to improve fill
                target = ecpm * 0.85
                recommended = round(max(target, DEFAULT_MIN_FLOOR), 2)
            else:
                continue

            change_pct = ((recommended - floor) / floor * 100) if floor > 0 else 0.0

            suggestions.append({
                "network": inst["network"],
                "geo_tier": inst["geo_tier"],
                "ad_format": inst["ad_format"],
                "time_block": inst["time_block"],
                "current_floor": floor,
                "recommended_floor": recommended,
                "ecpm": ecpm,
                "fill_rate": inst["fill_rate"],
                "change_pct": round(change_pct, 1),
                "estimated_daily_impact": 0.0,  # filled by estimate_revenue_impact
                "confidence": _suggestion_confidence(inst),
                "reason": inst["reason"],
                "requires_approval": True,
            })

        # Sort by absolute change magnitude descending
        suggestions.sort(key=lambda s: abs(s["change_pct"]), reverse=True)
        return suggestions

    # ── 3. Apply guardrails ───────────────────────────────────────────────

    @staticmethod
    def apply_guardrails(suggestions: list[dict]) -> list[dict]:
        """Enforce safety limits on floor-price suggestions.

        - Cap floor change at ±15 % per day.
        - Never go below per-network minimums.
        - Tag all as requiring manual approval (Phase 1).
        """
        guarded: list[dict] = []

        for s in suggestions:
            floor = s["current_floor"]
            rec = s["recommended_floor"]
            network = s["network"]

            # Cap change at ±15 %
            max_up = floor * (1 + MAX_FLOOR_CHANGE_PCT)
            max_down = floor * (1 - MAX_FLOOR_CHANGE_PCT)
            capped = max(max_down, min(max_up, rec))

            # Enforce network minimum
            net_min = NETWORK_FLOOR_MINIMUMS.get(network, DEFAULT_MIN_FLOOR)
            capped = max(net_min, capped)
            capped = round(capped, 2)

            # Recalculate change after capping
            new_change = ((capped - floor) / floor * 100) if floor > 0 else 0.0

            guarded_s = {**s}
            guarded_s["recommended_floor"] = capped
            guarded_s["change_pct"] = round(new_change, 1)
            guarded_s["guardrail_applied"] = (capped != rec)
            guarded_s["requires_approval"] = True
            guarded.append(guarded_s)

        return guarded

    # ── 4. Revenue impact estimation ──────────────────────────────────────

    @staticmethod
    def estimate_revenue_impact(
        suggestion: dict,
        daily_impressions: int = 0,
    ) -> float:
        """Estimate daily revenue change from a floor-price adjustment.

        Uses a simple fill-rate elasticity model:
        - Raising floor → lower fill rate → fewer but higher-paying impressions.
        - Lowering floor → higher fill rate → more but lower-paying impressions.

        Returns estimated daily revenue delta (USD).
        """
        current_floor = suggestion["current_floor"]
        new_floor = suggestion["recommended_floor"]
        ecpm = suggestion["ecpm"]
        fill_rate = suggestion["fill_rate"]

        if current_floor == 0 or new_floor == 0 or daily_impressions == 0:
            return 0.0

        floor_change_pct = (new_floor - current_floor) / current_floor

        # Elasticity: 1% floor increase → ~0.5% fill rate decrease
        fill_elasticity = -0.5
        new_fill = fill_rate * (1 + floor_change_pct * fill_elasticity)
        new_fill = max(0.05, min(1.0, new_fill))

        # New eCPM trends towards the new floor
        new_ecpm = ecpm + (new_floor - current_floor) * 0.3

        current_rev = (daily_impressions * fill_rate * ecpm) / 1000
        new_rev = (daily_impressions * new_fill * new_ecpm) / 1000

        return round(new_rev - current_rev, 2)

    # ── 5. Daily review pipeline ──────────────────────────────────────────

    def run_daily_waterfall_review(self) -> list[dict]:
        """Full daily waterfall optimization pipeline.

        a. Pull waterfall data
        b. Analyse instances
        c. Generate + guardrail suggestions
        d. Estimate revenue impacts
        e. Post to Slack #monetization-ops
        f. Save to Airtable
        g. Weekly Gmail report (Monday)
        """
        yesterday = (date.today() - timedelta(days=1)).isoformat()

        # a. Pull data
        instances = self._pull_data(yesterday)
        if not instances:
            logger.info("No waterfall data to analyse")
            return []

        # b. Analyse
        analysis = self.analyze_waterfall(instances)

        # c. Generate + guardrails
        suggestions = self.generate_suggestions(analysis)
        suggestions = self.apply_guardrails(suggestions)

        # d. Revenue impacts
        for s in suggestions:
            s["estimated_daily_impact"] = self.estimate_revenue_impact(
                s, daily_impressions=50_000
            )

        # e. Post to Slack
        self._post_to_slack(suggestions, analysis["summary"])

        # f. Save to Airtable
        self._save_to_airtable(instances)

        # g. Monday weekly report
        if date.today().weekday() == 0:
            self._send_weekly_report(suggestions)

        logger.info(
            "Waterfall review complete: %d suggestions (%d raise, %d lower)",
            len(suggestions),
            analysis["summary"]["raise"],
            analysis["summary"]["lower"],
        )
        return suggestions

    # ── Internal helpers ──────────────────────────────────────────────────

    @staticmethod
    def _pull_data(report_date: str) -> list[WaterfallInstance]:
        try:
            import asyncio
            from app.services.max_reporting import MaxReportingClient
            client = MaxReportingClient()
            return asyncio.get_event_loop().run_until_complete(
                client.pull_waterfall_data(report_date)
            )
        except Exception as exc:
            logger.warning("MAX data pull failed: %s", exc)
            return []

    @staticmethod
    def _post_to_slack(suggestions: list[dict], summary: dict) -> None:
        if not suggestions:
            return
        try:
            from app.services.slack_bot import SlackService
            slack = SlackService()
            for s in suggestions[:5]:  # Top 5 suggestions
                slack.post_waterfall_suggestion(s)
        except Exception as exc:
            logger.error("Slack waterfall post failed: %s", exc)

    @staticmethod
    def _save_to_airtable(instances: list[WaterfallInstance]) -> None:
        try:
            from app.services.airtable import AirtableService
            airtable = AirtableService()
            table = airtable._table("waterfall_state")
            rows = [
                {
                    "fields": {
                        "date": inst.date.isoformat() if inst.date else "",
                        "network": inst.network,
                        "geo_tier": inst.geo_tier,
                        "ad_format": inst.ad_format,
                        "floor_price": inst.floor_price,
                        "ecpm": inst.ecpm,
                        "fill_rate": inst.fill_rate,
                        "show_rate": inst.show_rate,
                        "revenue": inst.revenue,
                        "time_block": inst.time_block,
                    }
                }
                for inst in instances
            ]
            for i in range(0, len(rows), 10):
                table.batch_upsert(
                    rows[i:i + 10],
                    key_fields=["date", "network", "geo_tier", "ad_format", "time_block"],
                    typecast=True,
                )
        except Exception as exc:
            logger.error("Airtable waterfall save failed: %s", exc)

    @staticmethod
    def _send_weekly_report(suggestions: list[dict]) -> None:
        try:
            from app.services.gmail import GmailService
            from app.config import get_settings
            cfg = get_settings()
            gmail = GmailService()

            body_parts = ["<h1>Weekly Waterfall Optimization Report</h1>"]
            body_parts.append(f"<p>Generated: {date.today().isoformat()}</p>")
            body_parts.append("<table border='1' cellpadding='4' style='border-collapse:collapse;'>")
            body_parts.append(
                "<tr><th>Network</th><th>Geo</th><th>Format</th>"
                "<th>Current</th><th>Recommended</th><th>Change</th>"
                "<th>Impact</th><th>Confidence</th></tr>"
            )
            for s in suggestions:
                body_parts.append(
                    f"<tr><td>{s['network']}</td><td>{s['geo_tier']}</td>"
                    f"<td>{s['ad_format']}</td>"
                    f"<td>${s['current_floor']:.2f}</td>"
                    f"<td>${s['recommended_floor']:.2f}</td>"
                    f"<td>{s['change_pct']:+.1f}%</td>"
                    f"<td>${s.get('estimated_daily_impact', 0):.2f}/day</td>"
                    f"<td>{s['confidence']:.0f}%</td></tr>"
                )
            body_parts.append("</table>")

            gmail.create_draft(
                cfg.natasha_email,
                f"Waterfall Optimization Report — {date.today().isoformat()}",
                "\n".join(body_parts),
            )
        except Exception as exc:
            logger.error("Weekly waterfall email failed: %s", exc)


# ── Instance evaluation ──────────────────────────────────────────────────────

def _evaluate_instance(inst: WaterfallInstance) -> tuple[str, str]:
    """Return (signal, reason) for a single waterfall instance.

    signal: "raise", "lower", or "ok".
    """
    floor = inst.floor_price
    ecpm = inst.ecpm
    fill = inst.fill_rate

    # No floor set — can't optimise
    if floor <= 0:
        return ("ok", "No floor price set")

    # Fill rate too low with acceptable eCPM → floor too high
    if fill < FILL_RATE_LOW and ecpm >= floor * 0.7:
        return (
            "lower",
            f"Fill rate {fill:.0%} is below {FILL_RATE_LOW:.0%} "
            f"— floor ${floor:.2f} likely too high for eCPM ${ecpm:.2f}",
        )

    # eCPM well above floor → room to raise
    if ecpm > floor * ECPM_RAISE_RATIO:
        return (
            "raise",
            f"eCPM ${ecpm:.2f} is >{ECPM_RAISE_RATIO:.0%} of floor "
            f"${floor:.2f} — room to increase floor",
        )

    # eCPM below floor → floor too aggressive
    if ecpm < floor * ECPM_LOWER_RATIO:
        return (
            "lower",
            f"eCPM ${ecpm:.2f} is below {ECPM_LOWER_RATIO:.0%} of floor "
            f"${floor:.2f} — floor too aggressive",
        )

    # Fill rate very high → floor may be too low
    if fill > FILL_RATE_HIGH:
        return (
            "raise",
            f"Fill rate {fill:.0%} >{FILL_RATE_HIGH:.0%} — floor "
            f"${floor:.2f} may be leaving revenue on the table",
        )

    return ("ok", "Within acceptable range")


def _suggestion_confidence(inst: dict) -> float:
    """Estimate confidence (0-100) for a suggestion."""
    fill = inst.get("fill_rate", 0)
    ecpm = inst.get("ecpm", 0)
    floor = inst.get("floor_price", 0)

    # Base confidence
    conf = 60.0

    # Higher confidence when the signal is very clear
    if floor > 0:
        ratio = ecpm / floor
        if ratio > 1.5 or ratio < 0.7:
            conf += 20.0  # Strong signal
        elif ratio > 1.3 or ratio < 0.9:
            conf += 10.0

    # Fill rate extremes boost confidence
    if fill < 0.4 or fill > 0.98:
        conf += 10.0

    return min(100.0, conf)
