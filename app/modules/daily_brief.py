"""
Module 1 — Morning Intelligence Brief.

Orchestrates the full data pipeline that replaces Natasha's 45-60 minute
morning Tableau routine.  Pulls campaign/creative data, runs anomaly
detection, scans email/Slack, generates the brief via Claude, and
delivers it to Slack + Gmail.

Also provides an intraday spend-pacing check (every 2 hours 9AM-11PM IST).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from app.config import get_settings
from app.models.schemas import (
    Alert,
    AlertType,
    CampaignMetric,
    CreativeMetric,
    DailyBrief,
    Severity,
)

logger = logging.getLogger(__name__)

# Networks that should appear in every brief
_EXPECTED_NETWORKS = {"meta", "applovin", "google", "google ads", "google adwords"}

_PACING_DEVIATION_THRESHOLD = 0.20  # 20 % off-pace triggers alert


class DailyBriefModule:
    """Orchestrates the full morning-brief and intraday-check pipelines."""

    # ── 1. Main orchestrator ──────────────────────────────────────────────

    def run_daily_brief(self) -> DailyBrief:
        """Run the full morning intelligence brief pipeline.

        Steps:
          a. Pull yesterday's campaign + creative data (Singular)
          b. Load 14-day history (Airtable cache, fallback Singular)
          c. Run anomaly detection
          d. Run creative fatigue detection
          e. Scan Gmail + Slack context
          f. Load learning weights
          g. Compile data dict for Claude
          h. Generate brief via Claude
          i. Deliver to Slack + Gmail
          j. Log to Airtable
        """
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        today = date.today().isoformat()
        caveats: dict[str, Any] = {}

        # ── a/b. Pull data ────────────────────────────────────────────────
        campaign_data, creative_data, historical, creative_hist, caveats = (
            self._pull_all_data(yesterday, today, caveats)
        )

        # ── c. Anomaly detection ──────────────────────────────────────────
        anomalies = self._detect_anomalies(campaign_data, historical)

        # ── d. Creative fatigue ───────────────────────────────────────────
        creative_health = self._analyze_creatives(creative_data, creative_hist)

        # ── e. Email + Slack context ──────────────────────────────────────
        email_insights = self._scan_emails()
        slack_context = self._scan_slack()

        # ── f. Learning weights ───────────────────────────────────────────
        weights = self._load_weights()

        # ── g. Compile for Claude ─────────────────────────────────────────
        brief_data = self.compile_brief_data(
            campaign_data=campaign_data,
            creative_data=creative_data,
            historical=historical,
            anomalies=anomalies,
            emails=email_insights,
            slack_threads=slack_context,
            weights=weights,
            creative_health=creative_health,
            caveats=caveats,
        )

        # ── h. Generate brief ─────────────────────────────────────────────
        brief = self._generate_brief(brief_data, caveats)

        # ── i/j. Deliver + log ────────────────────────────────────────────
        self._deliver_brief(brief)

        # ── k. Persist campaign data to Airtable ─────────────────────────
        self._cache_data(campaign_data, creative_data)

        return brief

    # ── 2. Intraday check ─────────────────────────────────────────────────

    def run_intraday_check(self) -> list[Alert]:
        """Check spend pacing against daily budgets.

        Called every 2 hours from 9 AM to 11 PM IST.
        """
        today = date.today().isoformat()
        alerts: list[Alert] = []

        # a. Pull today's data
        try:
            from app.services.singular import SingularClient
            singular = SingularClient()
            import asyncio
            current = asyncio.get_event_loop().run_until_complete(
                singular.pull_campaign_data(today, today)
            )
        except Exception as exc:
            logger.warning("Intraday Singular pull failed: %s", exc)
            return []

        if not current:
            return []

        # b. Load historical daily averages for budget targets
        try:
            from app.services.airtable import AirtableService
            airtable = AirtableService()
            historical = airtable.get_recent_metrics(days=7)
        except Exception:
            historical = []

        # c. Compare pacing per network
        avg_by_network: dict[str, float] = defaultdict(float)
        count_by_network: dict[str, int] = defaultdict(int)
        for row in historical:
            avg_by_network[row.network] += row.spend
            count_by_network[row.network] += 1
        for net in avg_by_network:
            if count_by_network[net] > 0:
                avg_by_network[net] /= count_by_network[net]

        current_by_network: dict[str, float] = defaultdict(float)
        for row in current:
            current_by_network[row.network] += row.spend

        now = datetime.now(tz=timezone.utc)
        hour_fraction = max(now.hour / 24, 0.1)

        for network, current_spend in current_by_network.items():
            daily_avg = avg_by_network.get(network, 0)
            if daily_avg == 0:
                continue

            expected_at_this_hour = daily_avg * hour_fraction
            if expected_at_this_hour == 0:
                continue

            deviation = (current_spend - expected_at_this_hour) / expected_at_this_hour

            if abs(deviation) > _PACING_DEVIATION_THRESHOLD:
                direction = "over" if deviation > 0 else "under"
                severity = Severity.critical if abs(deviation) > 0.50 else Severity.high

                alert = Alert(
                    alert_id=f"pacing_{network}_{today}",
                    alert_type=AlertType.budget,
                    severity=severity,
                    title=f"Spend pacing {direction} on {network}",
                    body=(
                        f"{network}: ${current_spend:,.0f} spent so far today "
                        f"vs ${expected_at_this_hour:,.0f} expected "
                        f"({deviation:+.0%} deviation). "
                        f"Daily avg: ${daily_avg:,.0f}."
                    ),
                    related_campaign=network,
                    confidence=75.0,
                    timestamp=now,
                )
                alerts.append(alert)

        # d/e. Throttle + deliver
        self._deliver_alerts(alerts)

        return alerts

    # ── 3. Data compiler ──────────────────────────────────────────────────

    @staticmethod
    def compile_brief_data(
        campaign_data: list[CampaignMetric],
        creative_data: list[CreativeMetric],
        historical: list[CampaignMetric],
        anomalies: list[Alert],
        emails: list[dict],
        slack_threads: list[dict],
        weights: dict[str, float],
        creative_health: Optional[dict] = None,
        caveats: Optional[dict] = None,
    ) -> dict:
        """Aggregate all data sources into the format Claude expects."""
        caveats = caveats or {}

        # Aggregate campaign metrics by network
        network_metrics: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"spend": 0.0, "installs": 0, "cpi": 0.0, "roas_d7": 0.0, "count": 0}
        )
        for row in campaign_data:
            n = network_metrics[row.network]
            n["spend"] += row.spend
            n["installs"] += row.installs
            n["roas_d7"] += row.roas_d7
            n["count"] += 1

        # Average CPI/ROAS
        for net, m in network_metrics.items():
            if m["installs"] > 0:
                m["cpi"] = m["spend"] / m["installs"]
            if m["count"] > 0:
                m["roas_d7"] = m["roas_d7"] / m["count"]
            del m["count"]

        # Historical averages for comparison
        hist_network: dict[str, dict[str, float]] = defaultdict(
            lambda: {"spend_avg": 0.0, "count": 0.0}
        )
        for row in historical:
            h = hist_network[row.network]
            h["spend_avg"] += row.spend
            h["count"] += 1
        for net, h in hist_network.items():
            if h["count"] > 0:
                h["spend_avg"] /= h["count"]

        # Anomaly summaries
        anomaly_summaries = [
            {
                "title": a.title,
                "severity": a.severity.value,
                "campaign": a.related_campaign,
                "confidence": a.confidence,
            }
            for a in anomalies
        ]

        # Email summaries
        email_summaries = [
            {
                "sender": e.get("sender", ""),
                "subject": e.get("subject", ""),
                "category": e.get("category", ""),
                "insights": e.get("extracted_insights", ""),
            }
            for e in emails[:10]
        ]

        # Feedback-based confidence
        confidence = _calculate_confidence(
            data_completeness=_data_completeness(campaign_data, caveats),
            feedback_positive_rate=weights.get("_positive_rate", 0.5),
            historical_accuracy=weights.get("_historical_accuracy", 0.5),
        )

        # Check which expected networks are missing
        present = {r.network.lower() for r in campaign_data}
        missing = [n for n in _EXPECTED_NETWORKS if n not in present]

        return {
            "campaign_metrics": dict(network_metrics),
            "historical_averages": {
                k: v["spend_avg"] for k, v in hist_network.items()
            },
            "anomalies": anomaly_summaries,
            "email_insights": email_summaries,
            "slack_context": slack_threads,
            "feedback_history": weights,
            "creative_health": creative_health or {},
            "confidence": confidence,
            "revenue_unavailable": caveats.get("revenue_unavailable", False),
            "missing_networks": missing if missing else [],
            "using_cached_data": caveats.get("using_cached_data", False),
        }

    # ── 4. Graceful degradation ───────────────────────────────────────────

    def handle_brief_failure(self, error: Exception) -> DailyBrief:
        """Generate a degraded brief and notify via whichever channel works."""
        logger.error("Brief pipeline failed: %s", error)

        brief = DailyBrief(
            date=date.today(),
            spend_summary={},
            roas_tracker={},
            action_items=[
                "[HIGH] Daily brief generation failed — manual check required",
                f"[HIGH] Error: {str(error)[:200]}",
            ],
            inbox_context=[],
            system_confidence=0.0,
            raw_data={"error": str(error), "degraded": True},
        )

        # Try Slack
        try:
            from app.services.slack_bot import SlackService
            slack = SlackService()
            slack.dm_natasha(
                f":warning: *Daily brief failed*\n{str(error)[:300]}\n\n"
                "Please check Tableau manually this morning."
            )
        except Exception as slack_err:
            logger.error("Slack notification failed: %s", slack_err)

            # Try Gmail
            try:
                from app.services.gmail import GmailService
                gmail = GmailService()
                cfg = get_settings()
                gmail.send_email(
                    cfg.natasha_email,
                    "⚠ Mergedom Daily Brief Failed",
                    f"<p>The daily brief failed: {str(error)[:300]}</p>"
                    "<p>Please check Tableau manually.</p>",
                )
            except Exception as gmail_err:
                logger.error("Gmail notification failed: %s", gmail_err)

                # Last resort: Airtable log
                try:
                    from app.services.airtable import AirtableService
                    airtable = AirtableService()
                    airtable.save_email_context({
                        "email_id": f"brief_failure_{date.today().isoformat()}",
                        "sender": "system",
                        "subject": "Daily brief pipeline failure",
                        "extracted_insights": str(error)[:500],
                        "category": "system_error",
                    })
                except Exception:
                    logger.critical("ALL notification channels failed")

        return brief

    # ── Internal: data pulling ────────────────────────────────────────────

    def _pull_all_data(
        self, yesterday: str, today: str, caveats: dict
    ) -> tuple[list[CampaignMetric], list[CreativeMetric], list[CampaignMetric], list[CreativeMetric], dict]:
        """Pull campaign + creative + historical data."""
        campaign_data: list[CampaignMetric] = []
        creative_data: list[CreativeMetric] = []
        historical: list[CampaignMetric] = []
        creative_hist: list[CreativeMetric] = []

        # Try Singular first
        try:
            from app.services.singular import SingularClient
            import asyncio
            singular = SingularClient()
            campaign_data = asyncio.get_event_loop().run_until_complete(
                singular.pull_campaign_data(yesterday, yesterday)
            )
            creative_data = asyncio.get_event_loop().run_until_complete(
                singular.pull_creative_data(yesterday, yesterday)
            )
            if not singular.revenue_data_available:
                caveats["revenue_unavailable"] = True
        except Exception as exc:
            logger.warning("Singular pull failed, using cache: %s", exc)
            caveats["using_cached_data"] = True

        # Historical from Airtable (or Singular fallback)
        try:
            from app.services.airtable import AirtableService
            airtable = AirtableService()
            historical = airtable.get_recent_metrics(days=14)
            creative_hist = airtable.get_recent_creative_metrics(days=7)
        except Exception as exc:
            logger.warning("Airtable history read failed: %s", exc)

        # Fallback: if we have no data at all, try Singular for full range
        if not campaign_data and not historical:
            fourteen_ago = (date.today() - timedelta(days=14)).isoformat()
            try:
                from app.services.singular import SingularClient
                import asyncio
                singular = SingularClient()
                historical = asyncio.get_event_loop().run_until_complete(
                    singular.pull_campaign_data(fourteen_ago, yesterday)
                )
                caveats["using_cached_data"] = True
            except Exception:
                pass

        return campaign_data, creative_data, historical, creative_hist, caveats

    def _detect_anomalies(
        self,
        current: list[CampaignMetric],
        historical: list[CampaignMetric],
    ) -> list[Alert]:
        try:
            from app.pipelines.anomaly import AnomalyDetector
            detector = AnomalyDetector()
            weights = self._load_weights()
            return detector.detect_campaign_anomalies(current, historical, weights)
        except Exception as exc:
            logger.warning("Anomaly detection failed: %s", exc)
            return []

    def _analyze_creatives(
        self,
        creative_data: list[CreativeMetric],
        creative_hist: list[CreativeMetric],
    ) -> dict:
        try:
            from app.modules.creative import CreativeAnalyzer
            analyzer = CreativeAnalyzer()
            hist_by_id: dict[str, list[CreativeMetric]] = defaultdict(list)
            for cr in creative_hist:
                hist_by_id[cr.creative_id].append(cr)
            return analyzer.analyze_creative_health(creative_data, hist_by_id)
        except Exception as exc:
            logger.warning("Creative analysis failed: %s", exc)
            return {}

    def _scan_emails(self) -> list[dict]:
        try:
            from app.services.gmail import GmailService
            gmail = GmailService()
            emails = gmail.scan_inbox(hours=24)
            # Persist to Airtable
            try:
                from app.services.airtable import AirtableService
                airtable = AirtableService()
                for e in emails:
                    airtable.save_email_context(e)
            except Exception:
                pass
            return emails
        except Exception as exc:
            logger.warning("Gmail scan failed: %s", exc)
            return []

    def _scan_slack(self) -> list[dict]:
        # Slack thread scanning requires reading channel history,
        # which is handled by the reaction listener; return empty for now
        return []

    def _load_weights(self) -> dict[str, float]:
        try:
            from app.services.airtable import AirtableService
            return AirtableService().get_learning_weights()
        except Exception:
            return {}

    # ── Internal: brief generation ────────────────────────────────────────

    def _generate_brief(self, data: dict, caveats: dict) -> DailyBrief:
        """Generate brief via Claude with template fallback."""
        try:
            from app.services.claude import ClaudeService
            claude = ClaudeService()
            brief = claude.generate_daily_brief(data)
        except Exception as exc:
            logger.warning("Claude brief generation failed: %s", exc)
            brief = self._template_brief(data)

        # Inject caveats into raw_data
        brief.raw_data = brief.raw_data or {}
        brief.raw_data["revenue_unavailable"] = caveats.get("revenue_unavailable", False)
        brief.raw_data["missing_networks"] = data.get("missing_networks", [])
        brief.raw_data["using_cached_data"] = caveats.get("using_cached_data", False)

        # Override confidence with our calculated value
        if "confidence" in data:
            brief.system_confidence = data["confidence"]

        return brief

    @staticmethod
    def _template_brief(data: dict) -> DailyBrief:
        """Fallback brief built from templates (no Claude)."""
        metrics = data.get("campaign_metrics", {})

        spend_summary: dict[str, Any] = {}
        for net, m in metrics.items():
            spend_summary[net] = m.get("spend", 0) if isinstance(m, dict) else 0

        roas_tracker: dict[str, Any] = {}
        for net, m in metrics.items():
            roas_tracker[net] = m.get("roas_d7", 0) if isinstance(m, dict) else 0

        action_items: list[str] = []
        for a in data.get("anomalies", []):
            sev = a.get("severity", "medium").upper()
            action_items.append(f"[{sev}] {a.get('title', 'Alert')}")

        inbox = [
            e.get("insights", e.get("subject", ""))
            for e in data.get("email_insights", [])
        ]

        return DailyBrief(
            date=date.today(),
            spend_summary=spend_summary,
            roas_tracker=roas_tracker,
            action_items=action_items or ["No anomalies detected"],
            inbox_context=inbox,
            system_confidence=data.get("confidence", 0.0),
            raw_data={},
        )

    # ── Internal: delivery ────────────────────────────────────────────────

    def _deliver_brief(self, brief: DailyBrief) -> None:
        """Post brief to Slack, archive to Gmail, log to Airtable."""
        # Slack
        try:
            from app.services.slack_bot import SlackService
            SlackService().post_daily_brief(brief)
        except Exception as exc:
            logger.error("Slack brief delivery failed: %s", exc)

        # Gmail archive
        try:
            from app.services.gmail import GmailService
            GmailService().archive_brief(brief)
        except Exception as exc:
            logger.error("Gmail archive failed: %s", exc)

        # Airtable log
        try:
            from app.services.airtable import AirtableService
            AirtableService().save_email_context({
                "email_id": f"daily_brief_{brief.date}",
                "sender": "system",
                "subject": f"Daily Brief {brief.date}",
                "extracted_insights": str(brief.action_items),
                "category": "daily_brief",
            })
        except Exception as exc:
            logger.error("Airtable brief log failed: %s", exc)

    def _deliver_alerts(self, alerts: list[Alert]) -> None:
        """Deliver intraday alerts respecting throttle."""
        if not alerts:
            return

        try:
            from app.services.slack_bot import SlackService
            slack = SlackService()

            for alert in alerts:
                ts = slack.post_alert(alert)
                if ts:
                    # Log to Airtable
                    try:
                        from app.services.airtable import AirtableService
                        AirtableService().log_alert(alert)
                    except Exception:
                        pass

                # Critical: DM + email
                if alert.severity == Severity.critical:
                    slack.dm_natasha(
                        f":rotating_light: *{alert.title}*\n{alert.body}"
                    )
                    try:
                        from app.services.gmail import GmailService
                        cfg = get_settings()
                        GmailService().send_email(
                            cfg.natasha_email,
                            f"CRITICAL: {alert.title}",
                            f"<h2>{alert.title}</h2><p>{alert.body}</p>",
                        )
                    except Exception:
                        pass
        except Exception as exc:
            logger.error("Alert delivery failed: %s", exc)

    def _cache_data(
        self,
        campaign_data: list[CampaignMetric],
        creative_data: list[CreativeMetric],
    ) -> None:
        """Persist today's pull to Airtable for future historical queries."""
        try:
            from app.services.airtable import AirtableService
            airtable = AirtableService()
            if campaign_data:
                airtable.upsert_daily_metrics(campaign_data)
            if creative_data:
                airtable.upsert_creative_performance(creative_data)
        except Exception as exc:
            logger.warning("Airtable cache write failed: %s", exc)


# ── Confidence calculation ───────────────────────────────────────────────────

def _calculate_confidence(
    data_completeness: float,
    feedback_positive_rate: float,
    historical_accuracy: float,
) -> float:
    """Weighted confidence: data 30% + accuracy 40% + feedback 30%."""
    score = (
        data_completeness * 30
        + historical_accuracy * 40
        + feedback_positive_rate * 30
    )
    return round(min(100.0, max(0.0, score)), 1)


def _data_completeness(
    campaign_data: list[CampaignMetric], caveats: dict
) -> float:
    """Score 0.0–1.0 for how complete the data is."""
    if not campaign_data:
        return 0.0
    score = 0.5  # base: we have some data
    present = {r.network.lower() for r in campaign_data}
    covered = len(present & _EXPECTED_NETWORKS) / max(len(_EXPECTED_NETWORKS), 1)
    score += covered * 0.3
    if not caveats.get("revenue_unavailable"):
        score += 0.1
    if not caveats.get("using_cached_data"):
        score += 0.1
    return min(1.0, score)
