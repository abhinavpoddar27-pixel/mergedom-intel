"""
Module 5 — Cross-channel Deduplication & Escalation Coordinator.

Ensures every alert is delivered exactly once through the right channel,
with automatic escalation when acknowledgement is missing and
de-duplication across Slack, Gmail, and the daily brief.

Escalation matrix:
  CRITICAL → Slack #ua-alerts + DM Natasha; Gmail after 2 h if no reaction.
  HIGH     → Slack channel alert; included in next Gmail digest.
  MEDIUM   → Daily brief only (no standalone alert).
  LOW      → Suppressed (system learned to ignore).
"""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from app.models.schemas import Alert, AlertType, FeedbackEntry, Severity

logger = logging.getLogger(__name__)

# ── Channel names (mirror slack_bot.py) ──────────────────────────────────────

_CH_ALERTS = "#ua-alerts"
_CH_CREATIVE = "#creative-intel"
_CH_MONETIZATION = "#monetization-ops"
_CH_DAILY_BRIEF = "#ua-daily-brief"
_CH_GMAIL_DIGEST = "gmail_digest"
_CH_DM = "dm_natasha"

# Map alert_type → preferred Slack channel
_CHANNEL_MAP: dict[str, str] = {
    AlertType.anomaly.value: _CH_ALERTS,
    AlertType.budget.value: _CH_ALERTS,
    AlertType.creative_fatigue.value: _CH_CREATIVE,
    AlertType.waterfall.value: _CH_MONETIZATION,
    AlertType.system.value: _CH_ALERTS,
}

# Escalation timeout
_ESCALATION_HOURS = 2


class AlertCoordinator:
    """Routes, de-duplicates, and escalates alerts across channels."""

    def __init__(self) -> None:
        # In-memory dedup cache: {dedup_key: timestamp delivered}
        self._delivered: dict[str, datetime] = {}
        # Alerts pending escalation: {alert_id: (Alert, delivered_at)}
        self._pending_escalation: dict[str, tuple[Alert, datetime]] = {}

    # ── 1. Escalation matrix routing ──────────────────────────────────────

    def route_alert(self, alert: Alert) -> list[str]:
        """Determine which channels an alert should go to and deliver it.

        Returns the list of channel names the alert was sent to.
        """
        if self.check_duplicate(alert):
            logger.debug("Duplicate suppressed: %s", alert.alert_id)
            return []

        channels: list[str] = []

        if alert.severity == Severity.critical:
            channels = self._route_critical(alert)
        elif alert.severity == Severity.high:
            channels = self._route_high(alert)
        elif alert.severity == Severity.medium:
            channels = self._route_medium(alert)
        else:
            # LOW — suppressed
            logger.debug("LOW alert suppressed: %s", alert.alert_id)
            return []

        # Mark as delivered
        now = datetime.now(tz=timezone.utc)
        self._delivered[self._dedup_key(alert)] = now

        # Log to Airtable
        if channels:
            alert_copy = alert.model_copy(
                update={"channel_delivered": ", ".join(channels)}
            )
            self._log_alert(alert_copy)

        return channels

    def _route_critical(self, alert: Alert) -> list[str]:
        """CRITICAL: Slack alert + DM + pending Gmail escalation."""
        channels: list[str] = []

        # Slack #ua-alerts
        slack_ch = _CHANNEL_MAP.get(alert.alert_type.value, _CH_ALERTS)
        if self._post_slack(alert, slack_ch):
            channels.append(slack_ch)

        # DM Natasha
        if self._dm_natasha(alert):
            channels.append(_CH_DM)

        # Queue for Gmail escalation if no Slack reaction in 2 hours
        self._pending_escalation[alert.alert_id] = (
            alert, datetime.now(tz=timezone.utc)
        )

        return channels

    def _route_high(self, alert: Alert) -> list[str]:
        """HIGH: Slack channel alert + next Gmail digest."""
        channels: list[str] = []

        slack_ch = _CHANNEL_MAP.get(alert.alert_type.value, _CH_ALERTS)
        if self._post_slack(alert, slack_ch):
            channels.append(slack_ch)

        # Will be included in Gmail digest (tracked by Airtable)
        channels.append(_CH_GMAIL_DIGEST)
        return channels

    def _route_medium(self, alert: Alert) -> list[str]:
        """MEDIUM: daily brief only."""
        return [_CH_DAILY_BRIEF]

    # ── 2. Deduplication ──────────────────────────────────────────────────

    def check_duplicate(self, alert: Alert) -> bool:
        """Return True if this alert (or a substantially similar one)
        has already been delivered today."""
        key = self._dedup_key(alert)
        prev = self._delivered.get(key)
        if prev is None:
            return False
        # Same-day dedup
        return prev.date() == datetime.now(tz=timezone.utc).date()

    @staticmethod
    def _dedup_key(alert: Alert) -> str:
        """Hash of (campaign, alert_type, severity, date)."""
        raw = (
            f"{alert.related_campaign}:"
            f"{alert.alert_type.value}:"
            f"{alert.severity.value}:"
            f"{date.today().isoformat()}"
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    # ── 3. Slack-discussed check ──────────────────────────────────────────

    def check_slack_discussed(self, campaign: str) -> bool:
        """Return True if this campaign was recently discussed in Slack.

        Checks Airtable slack_context for threads mentioning the campaign
        from the last 24 hours.
        """
        try:
            from app.services.airtable import AirtableService
            airtable = AirtableService()
            # Use a simple formula check
            table = airtable._table("slack_context")
            yesterday = (date.today() - timedelta(days=1)).isoformat()
            formula = (
                f"AND(IS_AFTER({{date}}, '{yesterday}'), "
                f"FIND('{campaign}', {{related_campaigns}}))"
            )
            records = table.all(formula=formula)
            return len(records) > 0
        except Exception:
            return False

    # ── 4. Escalation ─────────────────────────────────────────────────────

    def escalate_if_needed(
        self, alert: Alert, hours_since: float = _ESCALATION_HOURS
    ) -> bool:
        """If a CRITICAL alert has no Slack reaction after *hours_since*,
        escalate via Gmail.  Returns True if escalation sent."""
        pending = self._pending_escalation.get(alert.alert_id)
        if not pending:
            return False

        _, delivered_at = pending
        elapsed = (datetime.now(tz=timezone.utc) - delivered_at).total_seconds() / 3600

        if elapsed < hours_since:
            return False

        # Check if there's been a reaction (via Airtable feedback_log)
        if self._has_feedback(alert.alert_id):
            del self._pending_escalation[alert.alert_id]
            return False

        # Escalate via Gmail
        self._send_gmail_escalation(alert)
        del self._pending_escalation[alert.alert_id]
        logger.info("Escalated alert %s via Gmail after %.1fh", alert.alert_id, elapsed)
        return True

    def check_pending_escalations(self) -> int:
        """Check all pending escalations.  Returns count escalated."""
        count = 0
        # Copy keys to avoid mutation during iteration
        for alert_id in list(self._pending_escalation.keys()):
            alert, _ = self._pending_escalation[alert_id]
            if self.escalate_if_needed(alert):
                count += 1
        return count

    # ── 5. Gmail digest compiler ──────────────────────────────────────────

    def compile_gmail_digest(self, alerts: list[Alert]) -> dict[str, Any]:
        """Compile a list of alerts into a structured Gmail digest.

        Groups by severity, marks reacted-to alerts as acknowledged,
        and flags Slack-discussed campaigns.
        """
        digest: dict[str, Any] = {
            "date": date.today().isoformat(),
            "sections": {
                "critical": [],
                "high": [],
                "resolved": [],
            },
            "total_alerts": len(alerts),
            "acknowledged_count": 0,
        }

        for alert in alerts:
            entry: dict[str, Any] = {
                "title": alert.title,
                "body": alert.body,
                "campaign": alert.related_campaign,
                "confidence": alert.confidence,
            }

            # Check if reacted to
            if self._has_feedback(alert.alert_id):
                entry["status"] = "acknowledged"
                digest["sections"]["resolved"].append(entry)
                digest["acknowledged_count"] += 1
                continue

            # Check if discussed in Slack
            if alert.related_campaign and self.check_slack_discussed(alert.related_campaign):
                entry["note"] = f"Already discussed in Slack"
                entry["status"] = "discussed"

            if alert.severity == Severity.critical:
                digest["sections"]["critical"].append(entry)
            else:
                digest["sections"]["high"].append(entry)

        return digest

    # ── Internal: service calls ───────────────────────────────────────────

    @staticmethod
    def _post_slack(alert: Alert, channel: str) -> bool:
        try:
            from app.services.slack_bot import SlackService
            ts = SlackService().post_alert(alert, channel=channel)
            return ts is not None
        except Exception as exc:
            logger.error("Slack post failed: %s", exc)
            return False

    @staticmethod
    def _dm_natasha(alert: Alert) -> bool:
        try:
            from app.services.slack_bot import SlackService
            ts = SlackService().dm_natasha(
                f":rotating_light: *CRITICAL: {alert.title}*\n{alert.body}"
            )
            return ts is not None
        except Exception as exc:
            logger.error("DM failed: %s", exc)
            return False

    @staticmethod
    def _log_alert(alert: Alert) -> None:
        try:
            from app.services.airtable import AirtableService
            AirtableService().log_alert(alert)
        except Exception as exc:
            logger.error("Airtable alert log failed: %s", exc)

    @staticmethod
    def _has_feedback(alert_id: str) -> bool:
        """Check Airtable feedback_log for any reaction on this alert."""
        try:
            from app.services.airtable import AirtableService
            airtable = AirtableService()
            table = airtable._table("feedback_log")
            records = table.all(formula=f"{{alert_id}}='{alert_id}'")
            return len(records) > 0
        except Exception:
            return False

    @staticmethod
    def _send_gmail_escalation(alert: Alert) -> None:
        try:
            from app.services.gmail import GmailService
            from app.config import get_settings
            cfg = get_settings()
            GmailService().send_email(
                cfg.natasha_email,
                f"ESCALATION: {alert.title}",
                (
                    f"<h2 style='color:red;'>{alert.title}</h2>"
                    f"<p>{alert.body}</p>"
                    f"<p><strong>No Slack reaction detected after "
                    f"{_ESCALATION_HOURS}h.</strong></p>"
                ),
            )
        except Exception as exc:
            logger.error("Gmail escalation failed: %s", exc)
