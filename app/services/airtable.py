"""
Airtable CRUD operations.

Provides typed methods for upserting campaign/creative metrics, logging
alerts and feedback, reading historical data, and managing learning
weights.  All operations go through pyairtable's ``Table`` API with
batch upsert support for idempotent writes.

Tables are referenced by the canonical names defined in ``TABLE_NAMES``.
The ``setup_airtable.py`` script creates them if they don't exist.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from pyairtable import Api
from pyairtable.api.table import Table

from app.config import get_settings
from app.models.schemas import (
    Alert,
    CampaignMetric,
    CreativeMetric,
    FeedbackEntry,
)

logger = logging.getLogger(__name__)

# ── Canonical table names ────────────────────────────────────────────────────

TABLE_NAMES = {
    "daily_metrics": "daily_metrics",
    "creative_performance": "creative_performance",
    "waterfall_state": "waterfall_state",
    "alert_log": "alert_log",
    "feedback_log": "feedback_log",
    "learning_weights": "learning_weights",
    "email_context": "email_context",
    "slack_context": "slack_context",
    "budget_curves": "budget_curves",
    "concept_taxonomy": "concept_taxonomy",
}

# Max records per pyairtable batch request
_BATCH_SIZE = 10  # Airtable limit per upsert call


# ── Helpers ──────────────────────────────────────────────────────────────────

def _date_str(d: date | None) -> str | None:
    """Airtable expects ISO-8601 date strings."""
    if d is None:
        return None
    return d.isoformat()


def _dt_str(dt: datetime | None) -> str | None:
    """Airtable expects ISO-8601 datetime strings."""
    if dt is None:
        return None
    return dt.isoformat()


def _safe_float(val: Any) -> float:
    try:
        return float(val) if val is not None else 0.0
    except (ValueError, TypeError):
        return 0.0


def _safe_int(val: Any) -> int:
    try:
        return int(float(val)) if val is not None else 0
    except (ValueError, TypeError):
        return 0


def _parse_date(val: Any) -> date | None:
    if val is None:
        return None
    if isinstance(val, date):
        return val
    try:
        return datetime.strptime(str(val)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


# ── Service class ────────────────────────────────────────────────────────────

class AirtableService:
    """High-level CRUD helper for the Mergedom Intel Airtable base."""

    def __init__(self) -> None:
        cfg = get_settings()
        self._api_key = cfg.airtable_api_key
        self._base_id = cfg.airtable_base_id

        if not self._api_key or not self._base_id:
            logger.warning(
                "Airtable credentials missing — service will degrade gracefully"
            )
            self._api: Api | None = None
            return

        self._api = Api(self._api_key)

    # ── Table accessor ────────────────────────────────────────────────────

    def _table(self, name: str) -> Table:
        if self._api is None:
            raise AirtableServiceError("Airtable API not initialised (missing credentials)")
        return self._api.table(self._base_id, TABLE_NAMES[name])

    # ── Daily metrics ─────────────────────────────────────────────────────

    def upsert_daily_metrics(self, records: list[CampaignMetric]) -> int:
        """Upsert campaign metric rows into the ``daily_metrics`` table.

        Keyed on (date, network, campaign_id, country) so re-runs for the
        same day are idempotent.  Returns the total number of records
        written.
        """
        table = self._table("daily_metrics")
        rows = [
            {
                "fields": {
                    "date": _date_str(r.date),
                    "network": r.network,
                    "campaign_id": r.campaign_id,
                    "campaign_name": r.campaign_name,
                    "os": r.os,
                    "country": r.country,
                    "spend": r.spend,
                    "installs": r.installs,
                    "impressions": r.impressions,
                    "clicks": r.clicks,
                    "cpi": r.cpi,
                    "ipm": r.ipm,
                    "ctr": r.ctr,
                    "roas_d1": r.roas_d1,
                    "roas_d7": r.roas_d7,
                    "roas_d30": r.roas_d30,
                    "revenue_d1": r.revenue_d1,
                    "revenue_d7": r.revenue_d7,
                    "revenue_d30": r.revenue_d30,
                }
            }
            for r in records
        ]

        written = self._batch_upsert(
            table, rows, key_fields=["date", "network", "campaign_id", "country"]
        )
        logger.info("Upserted %d daily_metrics rows", written)
        return written

    # ── Creative performance ──────────────────────────────────────────────

    def upsert_creative_performance(self, records: list[CreativeMetric]) -> int:
        """Upsert creative metric rows.  Keyed on (creative_id, platform)."""
        table = self._table("creative_performance")
        today_str = date.today().isoformat()
        rows = [
            {
                "fields": {
                    "date": today_str,
                    "creative_id": r.creative_id,
                    "creative_name": r.concept_tag,
                    "concept_tag": r.concept_tag,
                    "platform": r.platform,
                    "ipm": r.ipm,
                    "ctr": r.ctr,
                    "roas_d7": r.roas_d7,
                    "spend": r.spend,
                    "impressions": r.impressions,
                    "installs": r.installs,
                    "fatigue_score": r.fatigue_score,
                    "days_live": r.days_live,
                    "fatigue_curve_slope": r.fatigue_curve_slope,
                }
            }
            for r in records
        ]

        written = self._batch_upsert(
            table, rows, key_fields=["creative_id", "platform"]
        )
        logger.info("Upserted %d creative_performance rows", written)
        return written

    # ── Alert log ─────────────────────────────────────────────────────────

    def log_alert(self, alert: Alert) -> str:
        """Create a record in ``alert_log``.  Returns the Airtable record ID."""
        table = self._table("alert_log")
        record = table.create(
            {
                "alert_id": alert.alert_id,
                "type": alert.alert_type.value,
                "severity": alert.severity.value,
                "title": alert.title,
                "body": alert.body,
                "channel_delivered": alert.channel_delivered,
                "timestamp": _dt_str(alert.timestamp),
                "related_campaign": alert.related_campaign,
                "confidence": alert.confidence,
                "acted_on": False,
            },
            typecast=True,
        )
        rec_id = record["id"]
        logger.info("Logged alert %s → Airtable %s", alert.alert_id, rec_id)
        return rec_id

    # ── Feedback log ──────────────────────────────────────────────────────

    def log_feedback(self, feedback: FeedbackEntry) -> None:
        """Append a feedback entry to ``feedback_log``."""
        table = self._table("feedback_log")
        table.create(
            {
                "alert_id": feedback.alert_id,
                "reaction": feedback.reaction,
                "timestamp": _dt_str(feedback.timestamp),
                "action_taken": feedback.action_taken,
                "notes": "",
            },
            typecast=True,
        )
        logger.info("Logged feedback for alert %s", feedback.alert_id)

    # ── Learning weights ──────────────────────────────────────────────────

    def get_learning_weights(self) -> dict[str, float]:
        """Return ``{alert_type: weight}`` from the ``learning_weights`` table."""
        table = self._table("learning_weights")
        records = table.all()
        weights: dict[str, float] = {}
        for rec in records:
            f = rec["fields"]
            alert_type = f.get("alert_type", "")
            weight = _safe_float(f.get("weight", 1.0))
            if alert_type:
                weights[alert_type] = weight
        logger.debug("Loaded %d learning weights", len(weights))
        return weights

    def update_learning_weight(
        self,
        alert_type: str,
        new_weight: float,
        feedback_count: int | None = None,
        positive_rate: float | None = None,
    ) -> None:
        """Update (or create) the weight for a given alert type."""
        table = self._table("learning_weights")
        now_str = datetime.now(tz=timezone.utc).isoformat()

        fields: dict[str, Any] = {
            "alert_type": alert_type,
            "weight": new_weight,
            "last_updated": now_str,
        }
        if feedback_count is not None:
            fields["feedback_count"] = feedback_count
        if positive_rate is not None:
            fields["positive_rate"] = positive_rate

        table.batch_upsert(
            [{"fields": fields}],
            key_fields=["alert_type"],
            typecast=True,
        )
        logger.info("Updated learning weight %s → %.4f", alert_type, new_weight)

    # ── Historical reads ──────────────────────────────────────────────────

    def get_recent_metrics(self, days: int = 14) -> list[CampaignMetric]:
        """Read the last *days* of ``daily_metrics`` rows."""
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        formula = f"IS_AFTER({{date}}, '{cutoff}')"

        table = self._table("daily_metrics")
        records = table.all(formula=formula)

        metrics: list[CampaignMetric] = []
        for rec in records:
            f = rec["fields"]
            metrics.append(
                CampaignMetric(
                    date=_parse_date(f.get("date")),
                    network=str(f.get("network", "")),
                    campaign_id=str(f.get("campaign_id", "")),
                    campaign_name=str(f.get("campaign_name", "")),
                    os=str(f.get("os", "")),
                    country=str(f.get("country", "")),
                    spend=_safe_float(f.get("spend")),
                    installs=_safe_int(f.get("installs")),
                    impressions=_safe_int(f.get("impressions")),
                    clicks=_safe_int(f.get("clicks")),
                    cpi=_safe_float(f.get("cpi")),
                    ipm=_safe_float(f.get("ipm")),
                    ctr=_safe_float(f.get("ctr")),
                    roas_d1=_safe_float(f.get("roas_d1")),
                    roas_d7=_safe_float(f.get("roas_d7")),
                    roas_d30=_safe_float(f.get("roas_d30")),
                    revenue_d1=_safe_float(f.get("revenue_d1")),
                    revenue_d7=_safe_float(f.get("revenue_d7")),
                    revenue_d30=_safe_float(f.get("revenue_d30")),
                )
            )
        logger.info("Read %d recent daily_metrics rows (last %d days)", len(metrics), days)
        return metrics

    def get_recent_creative_metrics(self, days: int = 7) -> list[CreativeMetric]:
        """Read the last *days* of ``creative_performance`` rows."""
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        formula = f"IS_AFTER({{date}}, '{cutoff}')"

        table = self._table("creative_performance")
        records = table.all(formula=formula)

        metrics: list[CreativeMetric] = []
        for rec in records:
            f = rec["fields"]
            metrics.append(
                CreativeMetric(
                    creative_id=str(f.get("creative_id", "")),
                    concept_tag=str(f.get("concept_tag", "")),
                    platform=str(f.get("platform", "")),
                    ipm=_safe_float(f.get("ipm")),
                    ctr=_safe_float(f.get("ctr")),
                    roas_d7=_safe_float(f.get("roas_d7")),
                    spend=_safe_float(f.get("spend")),
                    impressions=_safe_int(f.get("impressions")),
                    installs=_safe_int(f.get("installs")),
                    fatigue_score=_safe_float(f.get("fatigue_score")),
                    days_live=_safe_int(f.get("days_live")),
                    fatigue_curve_slope=_safe_float(f.get("fatigue_curve_slope")),
                )
            )
        logger.info(
            "Read %d recent creative_performance rows (last %d days)",
            len(metrics), days,
        )
        return metrics

    # ── Context stores ────────────────────────────────────────────────────

    def save_email_context(self, email_data: dict) -> None:
        """Persist an extracted email context record."""
        table = self._table("email_context")
        table.create(
            {
                "email_id": str(email_data.get("email_id", "")),
                "sender": str(email_data.get("sender", "")),
                "subject": str(email_data.get("subject", "")),
                "extracted_insights": str(email_data.get("extracted_insights", "")),
                "related_campaign": str(email_data.get("related_campaign", "")),
                "date": _date_str(email_data.get("date")) or date.today().isoformat(),
                "category": str(email_data.get("category", "")),
            },
            typecast=True,
        )
        logger.info("Saved email context for %s", email_data.get("email_id"))

    def save_slack_context(self, thread_data: dict) -> None:
        """Persist an extracted Slack thread context record."""
        table = self._table("slack_context")
        table.create(
            {
                "thread_id": str(thread_data.get("thread_id", "")),
                "channel": str(thread_data.get("channel", "")),
                "summary": str(thread_data.get("summary", "")),
                "related_campaigns": str(thread_data.get("related_campaigns", "")),
                "date": _date_str(thread_data.get("date")) or date.today().isoformat(),
            },
            typecast=True,
        )
        logger.info("Saved Slack context for thread %s", thread_data.get("thread_id"))

    # ── Throttle helper ───────────────────────────────────────────────────

    def get_alerts_today(self) -> int:
        """Count alerts logged today — used for daily throttling."""
        today_str = date.today().isoformat()
        formula = f"IS_SAME({{timestamp}}, '{today_str}', 'day')"

        table = self._table("alert_log")
        records = table.all(formula=formula)
        count = len(records)
        logger.debug("Alerts today: %d", count)
        return count

    # ── Internal batch helper ─────────────────────────────────────────────

    @staticmethod
    def _batch_upsert(
        table: Table,
        rows: list[dict],
        key_fields: list[str],
    ) -> int:
        """Upsert rows in Airtable-compliant batch sizes.  Returns count."""
        total = 0
        for i in range(0, len(rows), _BATCH_SIZE):
            chunk = rows[i : i + _BATCH_SIZE]
            table.batch_upsert(chunk, key_fields=key_fields, typecast=True)
            total += len(chunk)
        return total


# ── Exceptions ───────────────────────────────────────────────────────────────

class AirtableServiceError(Exception):
    """Raised when an Airtable operation fails."""


# ── Standalone connectivity test ─────────────────────────────────────────────

if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    svc = AirtableService()
    if svc._api is None:
        print("ERROR: AIRTABLE_API_KEY / AIRTABLE_BASE_ID not set in .env")
        sys.exit(1)

    print(f"API key loaded ({len(svc._api_key)} chars), base: {svc._base_id}")

    try:
        base = svc._api.base(svc._base_id, validate=True)
        schema = base.schema()
        table_names = [t.name for t in schema.tables]
        print(f"Connected!  Found {len(table_names)} tables:")
        for name in sorted(table_names):
            print(f"  • {name}")
    except Exception as exc:
        print(f"Connection test failed: {exc}")
        sys.exit(1)
