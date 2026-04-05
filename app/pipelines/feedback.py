"""
Feedback loop processor.

Ingests Slack emoji reactions, recalculates adaptive learning weights,
computes priority scores, auto-suppresses noise, and generates a
weekly accuracy report for #system-feedback.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from app.models.schemas import Alert, FeedbackEntry, Severity

logger = logging.getLogger(__name__)

# ── Thresholds ───────────────────────────────────────────────────────────────

MIN_FEEDBACKS_FOR_RECALC = 20
SUPPRESSION_THRESHOLD = 0.10    # < 10 % positive rate
SUPPRESSION_MIN_FEEDBACKS = 30  # after 30+ feedbacks

_SEVERITY_SCORES = {
    Severity.critical: 4.0,
    Severity.high: 3.0,
    Severity.medium: 2.0,
    Severity.low: 1.0,
}


class FeedbackProcessor:
    """Processes reactions and adaptively tunes alert weights."""

    # ── 1. Process a reaction ─────────────────────────────────────────────

    def process_reaction(
        self, alert_id: str, reaction: str, user_id: str = ""
    ) -> None:
        """Ingest a Slack reaction and optionally trigger weight recalc.

        *reaction* should be one of: "useful", "noted", "not_useful".
        """
        feedback = FeedbackEntry(
            alert_id=alert_id,
            reaction=reaction,
            timestamp=datetime.now(tz=timezone.utc),
            action_taken=f"reaction_by_{user_id}" if user_id else "",
        )

        # Log to Airtable
        try:
            from app.services.airtable import AirtableService
            AirtableService().log_feedback(feedback)
        except Exception as exc:
            logger.error("Failed to log feedback: %s", exc)
            return

        # Resolve alert type from the original alert
        alert_type = self._resolve_alert_type(alert_id)
        if not alert_type:
            return

        # Check if we have enough data for weight recalc
        stats = self._get_feedback_stats(alert_type)
        if stats["total"] >= MIN_FEEDBACKS_FOR_RECALC:
            self._update_weight(alert_type, stats)

    # ── 2. Recalculate weights ────────────────────────────────────────────

    def recalculate_weights(self) -> dict[str, float]:
        """Recalculate learning weights for all alert types.

        Weight formula:
          positive_rate = useful_count / total_reactions
          raw_weight = positive_rate * (1 + log(feedback_count))
          Normalise so all weights sum to 1.

        Returns ``{alert_type: normalised_weight}``.
        """
        all_stats = self._get_all_stats()
        if not all_stats:
            return {}

        raw_weights: dict[str, float] = {}
        for alert_type, stats in all_stats.items():
            total = stats["total"]
            if total == 0:
                raw_weights[alert_type] = 1.0
                continue
            pos_rate = stats["useful"] / total
            raw = pos_rate * (1 + math.log(max(total, 1)))
            raw_weights[alert_type] = raw

        # Normalise
        total_raw = sum(raw_weights.values())
        if total_raw == 0:
            return {k: 1.0 / len(raw_weights) for k in raw_weights}

        normalised = {
            k: round(v / total_raw, 4) for k, v in raw_weights.items()
        }

        # Persist to Airtable
        try:
            from app.services.airtable import AirtableService
            airtable = AirtableService()
            for alert_type, weight in normalised.items():
                stats = all_stats.get(alert_type, {})
                airtable.update_learning_weight(
                    alert_type,
                    weight,
                    feedback_count=stats.get("total", 0),
                    positive_rate=stats.get("useful", 0) / max(stats.get("total", 1), 1),
                )
        except Exception as exc:
            logger.error("Weight persist failed: %s", exc)

        logger.info("Recalculated weights: %s", normalised)
        return normalised

    # ── 3. Priority scoring ───────────────────────────────────────────────

    @staticmethod
    def get_priority_score(alert: Alert, weights: dict[str, float]) -> float:
        """Combine severity with learned weight to produce a priority score.

        Higher score = more important.  Range roughly 0–10.
        """
        severity_score = _SEVERITY_SCORES.get(alert.severity, 1.0)
        weight = weights.get(alert.alert_type.value, 1.0)
        confidence_factor = alert.confidence / 100.0

        return round(severity_score * weight * (0.5 + confidence_factor * 0.5), 3)

    # ── 4. Suppress learned noise ─────────────────────────────────────────

    def suppress_learned_patterns(self) -> list[str]:
        """Identify alert types that should be auto-suppressed.

        Criteria: < 10 % positive rate after 30+ feedbacks.
        Returns the list of alert types that should be suppressed.
        """
        all_stats = self._get_all_stats()
        suppressed: list[str] = []

        for alert_type, stats in all_stats.items():
            total = stats["total"]
            if total < SUPPRESSION_MIN_FEEDBACKS:
                continue
            pos_rate = stats["useful"] / total if total > 0 else 0
            if pos_rate < SUPPRESSION_THRESHOLD:
                suppressed.append(alert_type)
                logger.info(
                    "Suppressing '%s': %.1f%% positive rate after %d feedbacks",
                    alert_type, pos_rate * 100, total,
                )

        return suppressed

    # ── 5. Weekly accuracy report ─────────────────────────────────────────

    def weekly_accuracy_report(self) -> dict[str, Any]:
        """Generate a weekly report on alert accuracy and post to Slack.

        Returns the report dict.
        """
        all_stats = self._get_all_stats()
        weights = self.recalculate_weights()
        suppressed = self.suppress_learned_patterns()

        # Build report
        per_type: list[dict] = []
        total_useful = 0
        total_all = 0
        for alert_type, stats in all_stats.items():
            total = stats["total"]
            useful = stats["useful"]
            total_useful += useful
            total_all += total
            pos_rate = useful / total if total > 0 else 0
            per_type.append({
                "alert_type": alert_type,
                "total": total,
                "useful": useful,
                "positive_rate": round(pos_rate * 100, 1),
                "weight": weights.get(alert_type, 0),
                "suppressed": alert_type in suppressed,
            })

        overall_rate = (total_useful / total_all * 100) if total_all > 0 else 0

        report = {
            "week_ending": date.today().isoformat(),
            "overall_useful_rate": round(overall_rate, 1),
            "total_alerts": total_all,
            "total_useful": total_useful,
            "per_type": per_type,
            "suppressed_types": suppressed,
            "weights": weights,
        }

        # Post to Slack #system-feedback
        self._post_report(report)

        return report

    # ── Internal: Airtable queries ────────────────────────────────────────

    @staticmethod
    def _resolve_alert_type(alert_id: str) -> Optional[str]:
        """Look up the alert_type for a given alert_id from Airtable."""
        try:
            from app.services.airtable import AirtableService
            airtable = AirtableService()
            table = airtable._table("alert_log")
            records = table.all(formula=f"{{alert_id}}='{alert_id}'")
            if records:
                return records[0]["fields"].get("type", "")
        except Exception:
            pass
        return None

    @staticmethod
    def _get_feedback_stats(alert_type: str) -> dict[str, int]:
        """Count reactions for a specific alert type."""
        try:
            from app.services.airtable import AirtableService
            airtable = AirtableService()

            # Get all alert_ids of this type
            alert_table = airtable._table("alert_log")
            alerts = alert_table.all(formula=f"{{type}}='{alert_type}'")
            alert_ids = {r["fields"].get("alert_id", "") for r in alerts}

            if not alert_ids:
                return {"total": 0, "useful": 0, "noted": 0, "not_useful": 0}

            # Get feedbacks for these alerts
            feedback_table = airtable._table("feedback_log")
            all_fb = feedback_table.all()

            counts = {"total": 0, "useful": 0, "noted": 0, "not_useful": 0}
            for fb in all_fb:
                f = fb["fields"]
                if f.get("alert_id", "") in alert_ids:
                    counts["total"] += 1
                    reaction = f.get("reaction", "")
                    if reaction in counts:
                        counts[reaction] += 1

            return counts
        except Exception:
            return {"total": 0, "useful": 0, "noted": 0, "not_useful": 0}

    @staticmethod
    def _get_all_stats() -> dict[str, dict[str, int]]:
        """Get feedback stats for all alert types at once."""
        try:
            from app.services.airtable import AirtableService
            airtable = AirtableService()

            # All alerts
            alert_table = airtable._table("alert_log")
            all_alerts = alert_table.all()

            # Map alert_id → type
            id_to_type: dict[str, str] = {}
            for r in all_alerts:
                f = r["fields"]
                id_to_type[f.get("alert_id", "")] = f.get("type", "unknown")

            # All feedbacks
            feedback_table = airtable._table("feedback_log")
            all_fb = feedback_table.all()

            stats: dict[str, dict[str, int]] = defaultdict(
                lambda: {"total": 0, "useful": 0, "noted": 0, "not_useful": 0}
            )

            for fb in all_fb:
                f = fb["fields"]
                aid = f.get("alert_id", "")
                atype = id_to_type.get(aid, "unknown")
                reaction = f.get("reaction", "")
                stats[atype]["total"] += 1
                if reaction in stats[atype]:
                    stats[atype][reaction] += 1

            return dict(stats)
        except Exception:
            return {}

    def _update_weight(self, alert_type: str, stats: dict[str, int]) -> None:
        """Recompute and persist a single alert type's weight."""
        total = stats["total"]
        if total == 0:
            return
        pos_rate = stats["useful"] / total
        raw = pos_rate * (1 + math.log(max(total, 1)))

        try:
            from app.services.airtable import AirtableService
            AirtableService().update_learning_weight(
                alert_type, round(raw, 4),
                feedback_count=total,
                positive_rate=round(pos_rate, 4),
            )
        except Exception as exc:
            logger.error("Weight update failed for %s: %s", alert_type, exc)

    @staticmethod
    def _post_report(report: dict) -> None:
        try:
            from app.services.slack_bot import SlackService
            slack = SlackService()

            lines = [
                f":bar_chart: *Weekly System Accuracy Report*",
                f"Week ending: {report['week_ending']}",
                f"Overall useful rate: *{report['overall_useful_rate']}%*",
                f"Total alerts: {report['total_alerts']} "
                f"({report['total_useful']} useful)",
            ]

            if report["suppressed_types"]:
                lines.append(
                    f":no_entry: Suppressed: {', '.join(report['suppressed_types'])}"
                )

            for pt in report["per_type"]:
                status = ":red_circle: suppressed" if pt["suppressed"] else ""
                lines.append(
                    f"  • {pt['alert_type']}: "
                    f"{pt['positive_rate']}% useful "
                    f"(w={pt['weight']:.3f}) {status}"
                )

            text = "\n".join(lines)
            slack._post("#system-feedback", "Weekly Accuracy Report", [
                {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            ])
        except Exception as exc:
            logger.error("Slack report post failed: %s", exc)
