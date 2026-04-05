"""
Formatters for Slack Block Kit, HTML email, and display utilities.

Provides standardised formatting for daily briefs, alerts, currency
(INR lakhs notation), percentage changes, and trend indicators used
across the Slack and Gmail delivery channels.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.models.schemas import Alert, DailyBrief, Severity


# ── Currency ─────────────────────────────────────────────────────────────────

def format_currency_inr(amount: float) -> str:
    """Format as INR-style shorthand.

    - < 1,000       → "₹950"
    - 1,000-99,999  → "₹45.2K"
    - 1,00,000+     → "₹8.2L"  (lakhs)
    - 1,00,00,000+  → "₹1.2Cr" (crores)

    Falls back to USD notation when amount is clearly USD (ad spend).
    """
    if amount < 0:
        return f"-{format_currency_inr(abs(amount))}"
    if amount >= 1_00_00_000:
        return f"₹{amount / 1_00_00_000:.1f}Cr"
    if amount >= 1_00_000:
        return f"₹{amount / 1_00_000:.1f}L"
    if amount >= 1_000:
        return f"₹{amount / 1_000:.1f}K"
    return f"₹{amount:,.0f}"


def format_usd(amount: float) -> str:
    """Format as USD with commas."""
    if abs(amount) >= 1_000:
        return f"${amount:,.0f}"
    return f"${amount:,.2f}"


# ── Percentage / trend ───────────────────────────────────────────────────────

def format_percentage_change(old: float, new: float) -> str:
    """Return a signed percentage string like '+12%' or '-8%'."""
    if old == 0:
        return "N/A" if new == 0 else "+∞%"
    pct = (new - old) / old * 100
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.0f}%"


def trend_arrow(change_pct: float) -> str:
    """Return a trend arrow + colour indicator for Slack mrkdwn.

    ↑↑  > +20%  (strong up)
    ↑   > +5%   (up)
    →   ± 5%    (flat)
    ↓   < -5%   (down)
    ↓↓  < -20%  (strong down)
    """
    if change_pct > 20:
        return ":chart_with_upwards_trend: ↑↑"
    if change_pct > 5:
        return ":small_red_triangle: ↑"
    if change_pct < -20:
        return ":chart_with_downwards_trend: ↓↓"
    if change_pct < -5:
        return ":small_red_triangle_down: ↓"
    return "→"


# ── Daily Brief → Slack Block Kit ────────────────────────────────────────────

def format_brief_slack(brief: DailyBrief) -> list[dict]:
    """Convert a ``DailyBrief`` into Slack Block Kit blocks."""
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
        lines: list[str] = []
        for network, val in spend.items():
            if isinstance(val, (int, float)):
                lines.append(f"• *{network}:* {format_usd(val)}")
            else:
                lines.append(f"• *{network}:* {val}")
        blocks.append(_section("*Spend Summary*\n" + "\n".join(lines)))

    # ROAS tracker
    roas = brief.roas_tracker
    if roas:
        lines = []
        for network, val in roas.items():
            if isinstance(val, (int, float)):
                lines.append(f"• *{network}:* {val:.2f}x")
            else:
                lines.append(f"• *{network}:* {val}")
        blocks.append(_section("*ROAS Tracker (D7)*\n" + "\n".join(lines)))

    # Action items
    if brief.action_items:
        items_text = "\n".join(f":point_right: {item}" for item in brief.action_items)
        blocks.append(_section(f"*Action Items*\n{items_text}"))

    # Inbox context
    if brief.inbox_context:
        inbox_text = "\n".join(f":email: {ctx}" for ctx in brief.inbox_context)
        blocks.append(_section(f"*Inbox Context*\n{inbox_text}"))

    # Confidence + caveats
    conf_text = f"System confidence: *{brief.system_confidence:.0f}%*"
    raw = brief.raw_data or {}
    caveats: list[str] = []
    if raw.get("revenue_unavailable"):
        caveats.append(":warning: Revenue data unavailable — analysis is CPI-based only")
    if raw.get("missing_networks"):
        nets = ", ".join(raw["missing_networks"])
        caveats.append(f":warning: Incomplete data for: {nets}")
    if raw.get("using_cached_data"):
        caveats.append(":hourglass: Using cached data — Singular API was unreachable")

    caveat_str = ("\n" + "\n".join(caveats)) if caveats else ""
    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": (
                f"{conf_text}{caveat_str}\n"
                "React :white_check_mark: useful · :eyes: noted · :x: not useful"
            ),
        }],
    })

    return blocks


# ── Daily Brief → HTML email ─────────────────────────────────────────────────

def format_brief_email(brief: DailyBrief) -> str:
    """Convert a ``DailyBrief`` into an HTML email body."""
    date_str = brief.date.isoformat() if brief.date else "Today"

    parts = [
        "<html><body style='font-family:Arial,sans-serif;max-width:600px;'>",
        f"<h1 style='color:#1a1a2e;'>Morning Brief — {date_str}</h1>",
        "<hr style='border:1px solid #e0e0e0;'>",
    ]

    # Spend
    spend = brief.spend_summary
    if spend:
        parts.append("<h2>Spend Summary</h2><table style='border-collapse:collapse;width:100%;'>")
        for network, val in spend.items():
            formatted = format_usd(val) if isinstance(val, (int, float)) else str(val)
            parts.append(
                f"<tr><td style='padding:4px 8px;border-bottom:1px solid #eee;'>"
                f"<strong>{network}</strong></td>"
                f"<td style='padding:4px 8px;border-bottom:1px solid #eee;"
                f"text-align:right;'>{formatted}</td></tr>"
            )
        parts.append("</table>")

    # ROAS
    roas = brief.roas_tracker
    if roas:
        parts.append("<h2>ROAS Tracker (D7)</h2><table style='border-collapse:collapse;width:100%;'>")
        for network, val in roas.items():
            formatted = f"{val:.2f}x" if isinstance(val, (int, float)) else str(val)
            parts.append(
                f"<tr><td style='padding:4px 8px;border-bottom:1px solid #eee;'>"
                f"<strong>{network}</strong></td>"
                f"<td style='padding:4px 8px;border-bottom:1px solid #eee;"
                f"text-align:right;'>{formatted}</td></tr>"
            )
        parts.append("</table>")

    # Actions
    if brief.action_items:
        parts.append("<h2>Action Items</h2><ol>")
        for item in brief.action_items:
            parts.append(f"<li>{item}</li>")
        parts.append("</ol>")

    # Inbox
    if brief.inbox_context:
        parts.append("<h2>Inbox Context</h2><ul>")
        for ctx in brief.inbox_context:
            parts.append(f"<li>{ctx}</li>")
        parts.append("</ul>")

    # Caveats
    raw = brief.raw_data or {}
    if raw.get("revenue_unavailable") or raw.get("missing_networks") or raw.get("using_cached_data"):
        parts.append("<h2 style='color:#c0392b;'>Data Caveats</h2><ul>")
        if raw.get("revenue_unavailable"):
            parts.append("<li>Revenue data unavailable — analysis is CPI-based only</li>")
        if raw.get("missing_networks"):
            parts.append(f"<li>Incomplete data for: {', '.join(raw['missing_networks'])}</li>")
        if raw.get("using_cached_data"):
            parts.append("<li>Using cached data — Singular API was unreachable</li>")
        parts.append("</ul>")

    # Confidence
    parts.append(
        f"<p style='color:#888;font-size:12px;'>"
        f"System confidence: {brief.system_confidence:.0f}%</p>"
    )
    parts.append("</body></html>")
    return "\n".join(parts)


# ── Alert → Slack Block Kit ──────────────────────────────────────────────────

def format_alert_slack(alert: Alert) -> list[dict]:
    """Convert an ``Alert`` into Slack Block Kit blocks."""
    severity_emoji = {
        Severity.critical: ":red_circle:",
        Severity.high: ":large_orange_circle:",
        Severity.medium: ":large_yellow_circle:",
        Severity.low: ":white_circle:",
    }
    emoji = severity_emoji.get(alert.severity, ":white_circle:")

    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{emoji} {alert.title}"},
        },
        _section(alert.body),
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


# ── Generic Slack Block Kit (kept for backward compatibility) ────────────────

def format_slack_blocks(payload: dict) -> list[dict]:
    """Generic dict-to-Block-Kit converter."""
    blocks: list[dict] = []
    if "title" in payload:
        blocks.append({
            "type": "header",
            "text": {"type": "plain_text", "text": str(payload["title"])},
        })
    if "body" in payload:
        blocks.append(_section(str(payload["body"])))
    return blocks


def format_email_html(payload: dict) -> str:
    """Generic dict-to-HTML converter."""
    title = payload.get("title", "Report")
    body = payload.get("body", "")
    return f"<html><body><h1>{title}</h1><p>{body}</p></body></html>"


# ── Private helpers ──────────────────────────────────────────────────────────

def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}
