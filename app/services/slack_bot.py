"""
Slack Bot service — powered by slack_bolt.

Handles all Slack interactions: posting formatted messages (Block Kit),
listening for emoji-reaction feedback, thread monitoring, alert
throttling, and DMs to the UA manager.

Runs in Socket Mode (development) or HTTP mode (production).
The reaction listener is started as a daemon thread so it never
blocks the main FastAPI event loop.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from app.config import get_settings
from app.models.schemas import (
    Alert,
    CreativeBrief,
    CreativeStatus,
    DailyBrief,
    FeedbackEntry,
    Severity,
)

logger = logging.getLogger(__name__)

# ── Channel map ──────────────────────────────────────────────────────────────

CHANNELS = {
    "daily_brief": "#ua-daily-brief",
    "alerts": "#ua-alerts",
    "creative": "#creative-intel",
    "monetization": "#monetization-ops",
    "feedback": "#system-feedback",
}

# ── Reaction → feedback mapping ──────────────────────────────────────────────

REACTION_MAP: dict[str, str] = {
    "white_check_mark": "useful",
    "heavy_check_mark": "useful",
    "+1": "useful",
    "thumbsup": "useful",
    "eyes": "noted",
    "x": "not_useful",
    "thumbsdown": "not_useful",
}

# ── Alert throttle settings ──────────────────────────────────────────────────

_BATCH_WINDOW_SECS = 30 * 60  # 30 minutes
_CRITICAL_SPEND_DEVIATION = 50  # % spend deviation that bypasses throttle


# ── Service class ────────────────────────────────────────────────────────────

class SlackService:
    """High-level Slack interface for the Mergedom Intel system."""

    def __init__(self) -> None:
        cfg = get_settings()
        self._bot_token = cfg.slack_bot_token
        self._app_token = cfg.slack_app_token
        self._signing_secret = cfg.slack_signing_secret
        self._natasha_user_id = cfg.natasha_slack_user_id

        if not self._bot_token:
            logger.warning("Slack bot token missing — service will degrade")
            self._client: Optional[WebClient] = None
            self._bolt_app: Optional[App] = None
            return

        self._client = WebClient(token=self._bot_token)
        self._bolt_app = App(
            token=self._bot_token,
            signing_secret=self._signing_secret,
        )
        self._register_handlers()

        # Throttle state
        self._pending_alerts: list[Alert] = []
        self._pending_since: float = 0.0
        self._lock = threading.Lock()

    # ── Posting: Daily Brief ──────────────────────────────────────────────

    def post_daily_brief(
        self, brief: DailyBrief, channel: str = CHANNELS["daily_brief"]
    ) -> Optional[str]:
        """Post the morning intelligence brief.  Returns message ``ts``."""
        blocks = self._format_brief_blocks(brief)
        return self._post(channel, "Daily Intelligence Brief", blocks)

    # ── Posting: Alerts ───────────────────────────────────────────────────

    def post_alert(
        self, alert: Alert, channel: str = CHANNELS["alerts"]
    ) -> Optional[str]:
        """Post a single alert card.  Returns message ``ts``.

        Respects the daily throttle (max 5/day) unless the alert is
        CRITICAL.  Alerts arriving within a 30-minute batch window are
        queued and sent together.
        """
        is_critical = alert.severity == Severity.critical

        if not is_critical and not self._check_throttle():
            logger.info("Alert throttled (daily limit): %s", alert.alert_id)
            return None

        # Batch non-critical alerts within the window
        if not is_critical:
            with self._lock:
                now = time.monotonic()
                if self._pending_alerts and (now - self._pending_since) < _BATCH_WINDOW_SECS:
                    self._pending_alerts.append(alert)
                    logger.info("Alert batched (%d pending)", len(self._pending_alerts))
                    return None
                elif self._pending_alerts:
                    # Window expired — flush batch + this alert
                    batch = self._pending_alerts + [alert]
                    self._pending_alerts = []
                    self._pending_since = 0.0
                    blocks = self._batch_alerts(batch)
                    return self._post(channel, f"{len(batch)} alerts", blocks)
                else:
                    # Start new batch window
                    self._pending_alerts = [alert]
                    self._pending_since = now
                    # Send immediately (first in window)

        blocks = self._format_alert_blocks(alert)
        ts = self._post(channel, alert.title, blocks)

        # DM Natasha for critical alerts
        if is_critical and self._natasha_user_id:
            self.dm_natasha(
                f":rotating_light: *CRITICAL ALERT*\n{alert.title}\n\n{alert.body}"
            )

        return ts

    def flush_pending_alerts(
        self, channel: str = CHANNELS["alerts"]
    ) -> Optional[str]:
        """Force-send any batched alerts.  Called by scheduler."""
        with self._lock:
            if not self._pending_alerts:
                return None
            batch = self._pending_alerts
            self._pending_alerts = []
            self._pending_since = 0.0

        blocks = self._batch_alerts(batch)
        return self._post(channel, f"{len(batch)} batched alerts", blocks)

    # ── Posting: Creative Brief ───────────────────────────────────────────

    def post_creative_brief(
        self, brief: CreativeBrief, channel: str = CHANNELS["creative"]
    ) -> Optional[str]:
        """Post a creative fatigue brief."""
        blocks = self._format_creative_blocks(brief)
        return self._post(channel, f"Creative Brief: {brief.concept_name}", blocks)

    # ── Posting: Waterfall Suggestion ─────────────────────────────────────

    def post_waterfall_suggestion(
        self, suggestion: dict[str, Any], channel: str = CHANNELS["monetization"]
    ) -> Optional[str]:
        """Post a waterfall optimization suggestion with approve/reject buttons."""
        blocks = self._format_waterfall_blocks(suggestion)
        return self._post(channel, "Waterfall Optimization Suggestion", blocks)

    # ── DM Natasha ────────────────────────────────────────────────────────

    def dm_natasha(self, message: str) -> Optional[str]:
        """Send a direct message to Natasha."""
        if not self._client or not self._natasha_user_id:
            logger.warning("Cannot DM Natasha — missing token or user ID")
            return None
        try:
            resp = self._client.chat_postMessage(
                channel=self._natasha_user_id,
                text=message,
            )
            return resp.get("ts")
        except SlackApiError as exc:
            logger.error("Failed to DM Natasha: %s", exc)
            return None

    # ── Reaction listener ─────────────────────────────────────────────────

    def start_reaction_listener(self) -> None:
        """Start the Bolt Socket Mode handler in a daemon thread.

        This is safe to call from the FastAPI lifespan — it will NOT
        block the event loop.
        """
        if not self._bolt_app or not self._app_token:
            logger.warning("Cannot start reaction listener — missing tokens")
            return

        handler = SocketModeHandler(self._bolt_app, self._app_token)
        thread = threading.Thread(
            target=handler.start,
            name="slack-bolt-listener",
            daemon=True,
        )
        thread.start()
        logger.info("Slack reaction listener started (daemon thread)")

    # ── Internal: handler registration ────────────────────────────────────

    def _register_handlers(self) -> None:
        """Wire up Bolt event handlers."""
        if not self._bolt_app:
            return

        @self._bolt_app.event("reaction_added")
        def handle_reaction(event: dict, say) -> None:  # noqa: ARG001
            self._on_reaction(event)

    def _on_reaction(self, event: dict) -> None:
        """Process a reaction_added event."""
        reaction = event.get("reaction", "")
        feedback_label = REACTION_MAP.get(reaction)
        if not feedback_label:
            return  # Not a tracked reaction

        item = event.get("item", {})
        msg_ts = item.get("ts", "")
        channel = item.get("channel", "")
        user = event.get("user", "")

        logger.info(
            "Reaction '%s' (%s) on %s/%s by %s",
            reaction, feedback_label, channel, msg_ts, user,
        )

        # Try to correlate with an alert_id (stored in message metadata)
        alert_id = self._resolve_alert_id(channel, msg_ts)

        feedback = FeedbackEntry(
            alert_id=alert_id or f"msg:{channel}:{msg_ts}",
            reaction=feedback_label,
            timestamp=datetime.now(tz=timezone.utc),
            action_taken="",
        )

        try:
            from app.services.airtable import AirtableService
            airtable = AirtableService()
            airtable.log_feedback(feedback)
        except Exception as exc:
            logger.error("Failed to log reaction feedback: %s", exc)

    def _resolve_alert_id(self, channel: str, msg_ts: str) -> Optional[str]:
        """Look up the alert_id from the message's Block Kit metadata."""
        if not self._client:
            return None
        try:
            resp = self._client.conversations_history(
                channel=channel, latest=msg_ts, limit=1, inclusive=True
            )
            messages = resp.get("messages", [])
            if messages:
                # Check for alert_id in block metadata or text
                msg = messages[0]
                metadata = msg.get("metadata", {})
                event_payload = metadata.get("event_payload", {})
                return event_payload.get("alert_id")
        except SlackApiError:
            pass
        return None

    # ── Internal: throttle check ──────────────────────────────────────────

    def _check_throttle(self) -> bool:
        """Return True if we can still send alerts today."""
        cfg = get_settings()
        max_per_day = cfg.max_alerts_per_day

        try:
            from app.services.airtable import AirtableService
            today_count = AirtableService().get_alerts_today()
        except Exception:
            today_count = 0

        if today_count >= max_per_day:
            logger.info(
                "Alert throttle hit: %d/%d sent today", today_count, max_per_day
            )
            return False
        return True

    # ── Internal: low-level post ──────────────────────────────────────────

    def _post(
        self,
        channel: str,
        fallback_text: str,
        blocks: list[dict],
        metadata: Optional[dict] = None,
    ) -> Optional[str]:
        """Post a message and return its ``ts``."""
        if not self._client:
            logger.warning("Slack client not initialised — message dropped")
            return None
        try:
            kwargs: dict[str, Any] = {
                "channel": channel,
                "text": fallback_text,
                "blocks": blocks,
            }
            if metadata:
                kwargs["metadata"] = metadata
            resp = self._client.chat_postMessage(**kwargs)
            ts = resp.get("ts")
            logger.info("Posted to %s (ts=%s)", channel, ts)
            return ts
        except SlackApiError as exc:
            logger.error("Slack post failed [%s]: %s", channel, exc)
            return None

    # ── Block Kit formatters ──────────────────────────────────────────────

    @staticmethod
    def _format_brief_blocks(brief: DailyBrief) -> list[dict]:
        """Format a DailyBrief as Slack Block Kit blocks."""
        date_str = brief.date.isoformat() if brief.date else "Today"

        blocks: list[dict] = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f":sunrise: Morning Brief — {date_str}",
                },
            },
        ]

        # Spend summary
        spend = brief.spend_summary
        if spend:
            lines = []
            for network, val in spend.items():
                lines.append(f"• *{network}:* ${val:,.2f}" if isinstance(val, (int, float)) else f"• *{network}:* {val}")
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*Spend Summary*\n" + "\n".join(lines)},
            })

        # ROAS tracker
        roas = brief.roas_tracker
        if roas:
            lines = []
            for network, val in roas.items():
                lines.append(f"• *{network}:* {val:.2f}x" if isinstance(val, (int, float)) else f"• *{network}:* {val}")
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*ROAS Tracker*\n" + "\n".join(lines)},
            })

        # Action items
        if brief.action_items:
            items_text = "\n".join(f":point_right: {item}" for item in brief.action_items)
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Action Items*\n{items_text}"},
            })

        # Inbox context
        if brief.inbox_context:
            inbox_text = "\n".join(f"• {ctx}" for ctx in brief.inbox_context)
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Inbox Context*\n{inbox_text}"},
            })

        # Confidence footer
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": f"System confidence: {brief.system_confidence:.0f}% | React :white_check_mark: useful · :eyes: noted · :x: not useful",
            }],
        })

        return blocks

    @staticmethod
    def _format_alert_blocks(alert: Alert) -> list[dict]:
        """Format a single Alert as Slack Block Kit blocks."""
        severity_emoji = {
            Severity.critical: ":red_circle:",
            Severity.high: ":large_orange_circle:",
            Severity.medium: ":large_yellow_circle:",
            Severity.low: ":white_circle:",
        }
        emoji = severity_emoji.get(alert.severity, ":white_circle:")

        blocks: list[dict] = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{emoji} {alert.title}",
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": alert.body,
                },
            },
            {
                "type": "context",
                "elements": [{
                    "type": "mrkdwn",
                    "text": (
                        f"Severity: *{alert.severity.value.upper()}* | "
                        f"Confidence: {alert.confidence:.0f}% | "
                        f"Campaign: {alert.related_campaign}"
                    ),
                }],
            },
            {"type": "divider"},
        ]

        return blocks

    @staticmethod
    def _batch_alerts(alerts: list[Alert]) -> list[dict]:
        """Format multiple alerts into a single batched message."""
        severity_emoji = {
            Severity.critical: ":red_circle:",
            Severity.high: ":large_orange_circle:",
            Severity.medium: ":large_yellow_circle:",
            Severity.low: ":white_circle:",
        }

        blocks: list[dict] = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f":bell: {len(alerts)} Alerts",
                },
            },
        ]

        for alert in alerts:
            emoji = severity_emoji.get(alert.severity, ":white_circle:")
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"{emoji} *{alert.title}*\n"
                        f"{alert.body[:200]}{'…' if len(alert.body) > 200 else ''}"
                    ),
                },
            })
            blocks.append({"type": "divider"})

        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": "React :white_check_mark: useful · :eyes: noted · :x: not useful",
            }],
        })

        return blocks

    @staticmethod
    def _format_creative_blocks(brief: CreativeBrief) -> list[dict]:
        """Format a CreativeBrief as Slack Block Kit blocks."""
        status_emoji = {
            CreativeStatus.active: ":large_green_circle:",
            CreativeStatus.fatigued: ":large_orange_circle:",
            CreativeStatus.paused: ":double_vertical_bar:",
            CreativeStatus.retired: ":red_circle:",
        }
        emoji = status_emoji.get(brief.status, ":white_circle:")

        blocks: list[dict] = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{emoji} Creative Brief: {brief.concept_name}",
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Status:* {brief.status.value.title()}\n"
                        f"*Platforms:* {', '.join(brief.platforms_affected) or 'N/A'}\n"
                        f"*What worked:* {brief.what_worked}"
                    ),
                },
            },
        ]

        if brief.recommendations:
            rec_text = "\n".join(f"• {r}" for r in brief.recommendations)
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Recommendations:*\n{rec_text}"},
            })

        return blocks

    @staticmethod
    def _format_waterfall_blocks(suggestion: dict[str, Any]) -> list[dict]:
        """Format a waterfall suggestion with approve/reject buttons."""
        network = suggestion.get("network", "Unknown")
        geo = suggestion.get("geo_tier", "")
        current_ecpm = suggestion.get("current_ecpm", 0)
        recommended_floor = suggestion.get("recommended_floor", 0)
        rationale = suggestion.get("rationale", "")
        suggestion_id = suggestion.get("id", "wf_0")

        blocks: list[dict] = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f":bar_chart: Waterfall Suggestion: {network}",
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Geo:* {geo}\n"
                        f"*Current eCPM:* ${current_ecpm:.2f}\n"
                        f"*Recommended floor:* ${recommended_floor:.2f}\n"
                        f"*Rationale:* {rationale}"
                    ),
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": ":white_check_mark: Approve"},
                        "style": "primary",
                        "action_id": f"waterfall_approve_{suggestion_id}",
                        "value": suggestion_id,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": ":x: Reject"},
                        "style": "danger",
                        "action_id": f"waterfall_reject_{suggestion_id}",
                        "value": suggestion_id,
                    },
                ],
            },
        ]

        return blocks
