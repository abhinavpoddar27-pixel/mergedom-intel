"""
Gmail API client.

Reads incoming emails from ad-network reps and platform alerts, extracts
actionable insights via Claude Haiku, and creates draft replies or
internal-only auto-sends for escalations.

SAFETY RULES
- External-facing emails always go to DRAFTS (never auto-sent).
- Only anomaly escalations addressed to the configured user email
  (``GMAIL_USER_EMAIL``) may be auto-sent.
- The ``send_email`` method explicitly verifies the recipient is internal
  before transmitting.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Optional

import anthropic
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from app.config import get_settings
from app.models.schemas import DailyBrief

logger = logging.getLogger(__name__)

# ── Scopes ───────────────────────────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify",
]

# ── Monitored sender domains ────────────────────────────────────────────────

NETWORK_REP_DOMAINS = {
    "meta.com",
    "facebook.com",
    "google.com",
    "applovin.com",
    "ironsource.com",
    "is.com",
    "unity.com",
    "unity3d.com",
}

PLATFORM_ALERT_SENDERS = {
    "google-ads-noreply@google.com",
    "noreply@business.facebook.com",
    "noreply@applovin.com",
}

# ── Email categories ─────────────────────────────────────────────────────────

CATEGORY_NETWORK_REP = "network_rep"
CATEGORY_PLATFORM_ALERT = "platform_alert"
CATEGORY_BILLING = "billing"
CATEGORY_CREATIVE_DELIVERY = "creative_delivery"
CATEGORY_STAKEHOLDER = "stakeholder_request"
CATEGORY_OTHER = "other"

_BILLING_KEYWORDS = {"invoice", "payment", "credit memo", "billing", "receipt", "remittance"}
_CREATIVE_KEYWORDS = {"creative", "asset", "delivery", "ad review", "disapproved"}

# ── UA Briefs label ──────────────────────────────────────────────────────────

UA_BRIEFS_LABEL = "UA Briefs"


class GmailService:
    """Gmail client for reading network emails and creating drafts/sends."""

    def __init__(self) -> None:
        cfg = get_settings()
        self._client_id = cfg.gmail_client_id
        self._client_secret = cfg.gmail_client_secret
        self._refresh_token = cfg.gmail_refresh_token
        self._user_email = cfg.gmail_user_email
        self._anthropic_key = cfg.anthropic_api_key
        self._claude_model = cfg.claude.model_parse  # haiku for parsing

        self._service = None
        if self._client_id and self._client_secret and self._refresh_token:
            self._service = self._build_service()
        else:
            logger.warning("Gmail credentials incomplete — service will degrade")

    # ── OAuth2 credential management ──────────────────────────────────────

    def _build_credentials(self) -> Credentials:
        """Build OAuth2 credentials from refresh token, auto-refreshing."""
        creds = Credentials(
            token=None,
            refresh_token=self._refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=self._client_id,
            client_secret=self._client_secret,
            scopes=SCOPES,
        )
        creds.refresh(Request())
        return creds

    def _build_service(self):
        """Build and return the Gmail API service object."""
        try:
            creds = self._build_credentials()
            return build("gmail", "v1", credentials=creds)
        except Exception as exc:
            logger.error("Failed to build Gmail service: %s", exc)
            return None

    def _ensure_service(self):
        if self._service is None:
            raise GmailServiceError("Gmail service not initialised (missing credentials)")
        return self._service

    # ── Read: inbox scan ──────────────────────────────────────────────────

    def scan_inbox(self, hours: int = 24) -> list[dict]:
        """Scan inbox for relevant emails from the last *hours*.

        Returns a list of parsed email dicts with keys:
        email_id, sender, subject, body, date, category,
        extracted_insights, related_campaign.
        """
        svc = self._ensure_service()

        # Build query: recent + from monitored domains
        domain_parts = " OR ".join(f"from:@{d}" for d in NETWORK_REP_DOMAINS)
        alert_parts = " OR ".join(f"from:{s}" for s in PLATFORM_ALERT_SENDERS)
        query = f"newer_than:{hours}h AND ({domain_parts} OR {alert_parts})"

        logger.info("Scanning Gmail inbox: %s", query)

        try:
            results = (
                svc.users()
                .messages()
                .list(userId="me", q=query, maxResults=50)
                .execute()
            )
        except Exception as exc:
            logger.error("Gmail list failed: %s", exc)
            return []

        messages = results.get("messages", [])
        if not messages:
            logger.info("No matching emails found")
            return []

        parsed: list[dict] = []
        for msg_stub in messages:
            msg_id = msg_stub["id"]
            try:
                email_data = self._parse_message(msg_id)
                if email_data:
                    # Classify and extract insights
                    email_data["category"] = self._classify_email(email_data)
                    insights = self._extract_insights(email_data.get("body", ""))
                    email_data["extracted_insights"] = insights.get("summary", "")
                    email_data["related_campaign"] = insights.get("related_campaign", "")
                    parsed.append(email_data)
            except Exception as exc:
                logger.warning("Failed to parse message %s: %s", msg_id, exc)

        logger.info("Parsed %d emails from inbox scan", len(parsed))
        return parsed

    def _parse_message(self, msg_id: str) -> Optional[dict]:
        """Fetch and parse a single Gmail message."""
        svc = self._ensure_service()

        msg = (
            svc.users()
            .messages()
            .get(userId="me", id=msg_id, format="full")
            .execute()
        )

        headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}

        sender = headers.get("from", "")
        subject = headers.get("subject", "")
        date_str = headers.get("date", "")
        thread_id = msg.get("threadId", "")

        body = self._extract_body(msg.get("payload", {}))

        return {
            "email_id": msg_id,
            "thread_id": thread_id,
            "sender": sender,
            "subject": subject,
            "body": body,
            "date": date_str,
        }

    @staticmethod
    def _extract_body(payload: dict) -> str:
        """Recursively extract plain-text body from MIME payload."""
        mime_type = payload.get("mimeType", "")

        if mime_type == "text/plain":
            data = payload.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

        # Recurse into parts
        for part in payload.get("parts", []):
            text = GmailService._extract_body(part)
            if text:
                return text

        # Fallback: try html
        if mime_type == "text/html":
            data = payload.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

        return ""

    # ── Classification ────────────────────────────────────────────────────

    @staticmethod
    def _classify_email(email_data: dict) -> str:
        """Classify an email into a category based on sender and content."""
        sender = email_data.get("sender", "").lower()
        subject = email_data.get("subject", "").lower()
        body_lower = email_data.get("body", "")[:500].lower()

        # Platform automated alerts
        for alert_sender in PLATFORM_ALERT_SENDERS:
            if alert_sender in sender:
                return CATEGORY_PLATFORM_ALERT

        # Billing / finance
        if any(kw in subject or kw in body_lower for kw in _BILLING_KEYWORDS):
            return CATEGORY_BILLING

        # Creative delivery
        if any(kw in subject or kw in body_lower for kw in _CREATIVE_KEYWORDS):
            return CATEGORY_CREATIVE_DELIVERY

        # Network rep
        for domain in NETWORK_REP_DOMAINS:
            if domain in sender:
                return CATEGORY_NETWORK_REP

        return CATEGORY_OTHER

    # ── Claude insight extraction ─────────────────────────────────────────

    def _extract_insights(self, email_body: str) -> dict:
        """Use Claude Haiku to extract insights from an email body.

        Returns dict with keys: summary, action_items, related_campaign,
        policy_changes, credits, deadlines.
        """
        if not self._anthropic_key or not email_body.strip():
            return {"summary": "", "related_campaign": ""}

        # Truncate very long emails
        body_truncated = email_body[:3000]

        prompt = (
            "You are a mobile gaming UA analyst. Extract insights from "
            "this email from an ad network or platform.\n\n"
            "Return ONLY valid JSON with these keys:\n"
            '- "summary": one-sentence summary of what matters\n'
            '- "action_items": list of things to do\n'
            '- "related_campaign": campaign name if mentioned, else ""\n'
            '- "policy_changes": any policy/TOS changes mentioned, else ""\n'
            '- "credits": any credits/rebates offered, else ""\n'
            '- "deadlines": any deadlines, else ""\n\n'
            f"Email body:\n{body_truncated}"
        )

        try:
            import json
            client = anthropic.Anthropic(api_key=self._anthropic_key)
            resp = client.messages.create(
                model=self._claude_model,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text
            start = text.index("{")
            end = text.rindex("}") + 1
            return json.loads(text[start:end])
        except Exception as exc:
            logger.warning("Claude insight extraction failed: %s", exc)
            return {"summary": "", "related_campaign": ""}

    # ── Write: drafts ─────────────────────────────────────────────────────

    def create_draft(
        self,
        to: str,
        subject: str,
        body_html: str,
        thread_id: Optional[str] = None,
    ) -> str:
        """Create a draft email.  Returns the draft ID.

        All external-facing emails MUST use this method (never
        ``send_email``).
        """
        svc = self._ensure_service()
        message = self._build_mime(to, subject, body_html)

        draft_body: dict[str, Any] = {"message": {"raw": message}}
        if thread_id:
            draft_body["message"]["threadId"] = thread_id

        draft = svc.users().drafts().create(userId="me", body=draft_body).execute()
        draft_id = draft.get("id", "")
        logger.info("Created draft %s → %s: %s", draft_id, to, subject)
        return draft_id

    # ── Write: send (internal only!) ──────────────────────────────────────

    def send_email(self, to: str, subject: str, body_html: str) -> str:
        """Send an email.  ONLY for internal recipients.

        Raises ``GmailServiceError`` if *to* is not the configured user
        email (safety guard against accidental external sends).
        """
        # Safety: only send to Natasha's own address
        if not self._is_internal_recipient(to):
            raise GmailServiceError(
                f"Refusing to auto-send to external address: {to}. "
                f"Use create_draft() instead."
            )

        svc = self._ensure_service()
        message = self._build_mime(to, subject, body_html)

        sent = (
            svc.users()
            .messages()
            .send(userId="me", body={"raw": message})
            .execute()
        )
        msg_id = sent.get("id", "")
        logger.info("Sent email %s → %s: %s", msg_id, to, subject)
        return msg_id

    def _is_internal_recipient(self, to: str) -> bool:
        """Check if *to* is the configured user email."""
        if not self._user_email:
            return False
        return to.strip().lower() == self._user_email.strip().lower()

    # ── Brief archival ────────────────────────────────────────────────────

    def archive_brief(self, brief: DailyBrief) -> str:
        """Create a draft of the daily brief and label it 'UA Briefs'.

        Returns the draft ID.
        """
        date_str = brief.date.isoformat() if brief.date else "today"
        subject = f"Mergedom UA Brief — {date_str}"
        html = self._build_html_report(
            {
                "title": subject,
                "spend_summary": brief.spend_summary,
                "roas_tracker": brief.roas_tracker,
                "action_items": brief.action_items,
                "inbox_context": brief.inbox_context,
                "confidence": brief.system_confidence,
            },
            template="daily_brief",
        )

        draft_id = self.create_draft(self._user_email, subject, html)

        # Apply UA Briefs label
        try:
            label_id = self.get_label_id(UA_BRIEFS_LABEL)
            if label_id and draft_id:
                svc = self._ensure_service()
                draft = svc.users().drafts().get(userId="me", id=draft_id).execute()
                msg_id = draft.get("message", {}).get("id")
                if msg_id:
                    svc.users().messages().modify(
                        userId="me",
                        id=msg_id,
                        body={"addLabelIds": [label_id]},
                    ).execute()
        except Exception as exc:
            logger.warning("Failed to apply UA Briefs label: %s", exc)

        return draft_id

    # ── Labels ────────────────────────────────────────────────────────────

    def get_label_id(self, label_name: str) -> Optional[str]:
        """Get a label's ID, creating it if it doesn't exist."""
        svc = self._ensure_service()

        try:
            results = svc.users().labels().list(userId="me").execute()
            for label in results.get("labels", []):
                if label["name"] == label_name:
                    return label["id"]

            # Create label
            new_label = (
                svc.users()
                .labels()
                .create(
                    userId="me",
                    body={
                        "name": label_name,
                        "labelListVisibility": "labelShow",
                        "messageListVisibility": "show",
                    },
                )
                .execute()
            )
            label_id = new_label.get("id", "")
            logger.info("Created label '%s' (id=%s)", label_name, label_id)
            return label_id
        except Exception as exc:
            logger.error("Label operation failed: %s", exc)
            return None

    # ── HTML report builder ───────────────────────────────────────────────

    @staticmethod
    def _build_html_report(data: dict, template: str = "daily_brief") -> str:
        """Build an HTML email body from a data dict and template name."""
        title = data.get("title", "Mergedom UA Report")

        sections = [
            f"<html><body>"
            f"<h1 style='color:#1a1a2e;'>{title}</h1>"
            f"<hr style='border:1px solid #e0e0e0;'>"
        ]

        # Spend summary
        spend = data.get("spend_summary", {})
        if spend:
            sections.append("<h2>Spend Summary</h2><table style='border-collapse:collapse;width:100%;'>")
            for network, val in spend.items():
                formatted = f"${val:,.2f}" if isinstance(val, (int, float)) else str(val)
                sections.append(
                    f"<tr><td style='padding:4px 8px;border-bottom:1px solid #eee;'>"
                    f"<strong>{network}</strong></td>"
                    f"<td style='padding:4px 8px;border-bottom:1px solid #eee;text-align:right;'>"
                    f"{formatted}</td></tr>"
                )
            sections.append("</table>")

        # ROAS tracker
        roas = data.get("roas_tracker", {})
        if roas:
            sections.append("<h2>ROAS Tracker</h2><table style='border-collapse:collapse;width:100%;'>")
            for network, val in roas.items():
                formatted = f"{val:.2f}x" if isinstance(val, (int, float)) else str(val)
                sections.append(
                    f"<tr><td style='padding:4px 8px;border-bottom:1px solid #eee;'>"
                    f"<strong>{network}</strong></td>"
                    f"<td style='padding:4px 8px;border-bottom:1px solid #eee;text-align:right;'>"
                    f"{formatted}</td></tr>"
                )
            sections.append("</table>")

        # Action items
        actions = data.get("action_items", [])
        if actions:
            sections.append("<h2>Action Items</h2><ul>")
            for item in actions:
                sections.append(f"<li>{item}</li>")
            sections.append("</ul>")

        # Inbox context
        inbox = data.get("inbox_context", [])
        if inbox:
            sections.append("<h2>Inbox Context</h2><ul>")
            for ctx in inbox:
                sections.append(f"<li>{ctx}</li>")
            sections.append("</ul>")

        # Confidence
        conf = data.get("confidence", 0)
        if conf:
            sections.append(
                f"<p style='color:#888;font-size:12px;'>"
                f"System confidence: {conf:.0f}%</p>"
            )

        sections.append("</body></html>")
        return "\n".join(sections)

    # ── Internal helpers ──────────────────────────────────────────────────

    def _build_mime(self, to: str, subject: str, body_html: str) -> str:
        """Build a base64url-encoded MIME message."""
        msg = MIMEMultipart("alternative")
        msg["to"] = to
        msg["from"] = self._user_email
        msg["subject"] = subject
        msg.attach(MIMEText(body_html, "html"))
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        return raw


# ── Exceptions ───────────────────────────────────────────────────────────────

class GmailServiceError(Exception):
    """Raised when a Gmail operation fails."""


# ── Standalone connectivity test ─────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    svc = GmailService()
    if svc._service is None:
        print("ERROR: Gmail credentials not configured in .env")
        print("Run: python scripts/setup_gmail.py")
        sys.exit(1)

    print("Gmail service initialised — testing inbox scan …")
    try:
        emails = svc.scan_inbox(hours=48)
        print(f"Found {len(emails)} relevant emails")
        for e in emails[:3]:
            print(f"  [{e.get('category')}] {e.get('sender')}: {e.get('subject')}")
    except GmailServiceError as exc:
        print(f"Error: {exc}")
        sys.exit(1)
