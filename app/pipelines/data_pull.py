"""
Data pull orchestrator.

Coordinates API calls across Singular, Meta, AppLovin, Google Ads, MAX,
Gmail, and Slack to assemble a unified performance dataset.  Each source
is pulled independently — one failure never crashes the whole pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.config import get_settings
from app.models.schemas import CampaignMetric, CreativeMetric, WaterfallInstance

logger = logging.getLogger(__name__)


class SourceResult:
    """Outcome of a single data-source pull."""

    __slots__ = ("name", "success", "rows", "elapsed", "error")

    def __init__(self, name: str) -> None:
        self.name = name
        self.success = False
        self.rows = 0
        self.elapsed = 0.0
        self.error = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "success": self.success,
            "rows": self.rows,
            "elapsed_s": round(self.elapsed, 2),
            "error": self.error,
        }


class DataPullPipeline:
    """Orchestrates all data ingestion for the intelligence system."""

    # ── 1. Pull all sources ───────────────────────────────────────────────

    def pull_all_sources(self, report_date: str | None = None) -> dict[str, Any]:
        """Pull every configured data source for *report_date*.

        Returns a compiled dict with:
          campaign_data, creative_data, waterfall_data,
          meta_data, applovin_data, google_data,
          email_insights, slack_context,
          source_results (metadata per source),
          revenue_unavailable (bool).
        """
        if report_date is None:
            report_date = (date.today() - timedelta(days=1)).isoformat()

        results: dict[str, Any] = {
            "report_date": report_date,
            "campaign_data": [],
            "creative_data": [],
            "waterfall_data": [],
            "meta_campaigns": [],
            "meta_creatives": [],
            "applovin_campaigns": [],
            "applovin_creatives": [],
            "google_campaigns": [],
            "google_ads": [],
            "email_insights": [],
            "slack_context": [],
            "source_results": [],
            "revenue_unavailable": False,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }

        # Run pulls (sync wrappers around async calls)
        pulls = [
            ("Singular campaigns", self._pull_singular_campaigns, report_date),
            ("Singular creatives", self._pull_singular_creatives, report_date),
            ("Meta campaigns", self._pull_meta, report_date),
            ("AppLovin campaigns", self._pull_applovin, report_date),
            ("Google Ads", self._pull_google, report_date),
            ("MAX mediation", self._pull_max, report_date),
            ("Gmail inbox", self._pull_gmail, None),
            ("Slack context", self._pull_slack, None),
        ]

        for name, fn, arg in pulls:
            sr = SourceResult(name)
            t0 = time.monotonic()
            try:
                data = fn(arg) if arg is not None else fn()
                sr.success = True
                sr.rows = len(data) if isinstance(data, list) else 1
                self._store_result(results, name, data)
            except Exception as exc:
                sr.error = str(exc)[:200]
                logger.warning("Data pull failed [%s]: %s", name, exc)
            sr.elapsed = time.monotonic() - t0
            results["source_results"].append(sr.to_dict())

        # Check revenue flag from Singular
        try:
            from app.services.singular import SingularClient
            if hasattr(self, "_singular_client") and not self._singular_client.revenue_data_available:
                results["revenue_unavailable"] = True
        except Exception:
            pass

        succeeded = sum(1 for s in results["source_results"] if s["success"])
        total = len(results["source_results"])
        logger.info(
            "Data pull complete: %d/%d sources succeeded for %s",
            succeeded, total, report_date,
        )
        return results

    # ── 2. Normalize and store ────────────────────────────────────────────

    @staticmethod
    def normalize_and_store(raw_data: dict[str, Any]) -> dict[str, int]:
        """Convert raw pull results to schema models and upsert to Airtable.

        Returns ``{table: row_count}`` of records written.
        """
        counts: dict[str, int] = {}

        try:
            from app.services.airtable import AirtableService
            airtable = AirtableService()
        except Exception as exc:
            logger.error("Airtable init failed — cannot store: %s", exc)
            return counts

        # Campaign metrics
        campaigns: list[CampaignMetric] = raw_data.get("campaign_data", [])
        if campaigns:
            try:
                n = airtable.upsert_daily_metrics(campaigns)
                counts["daily_metrics"] = n
            except Exception as exc:
                logger.error("Campaign upsert failed: %s", exc)

        # Creative metrics
        creatives: list[CreativeMetric] = raw_data.get("creative_data", [])
        if creatives:
            try:
                n = airtable.upsert_creative_performance(creatives)
                counts["creative_performance"] = n
            except Exception as exc:
                logger.error("Creative upsert failed: %s", exc)

        # Email context
        for email in raw_data.get("email_insights", []):
            try:
                airtable.save_email_context(email)
                counts["email_context"] = counts.get("email_context", 0) + 1
            except Exception:
                pass

        logger.info("Stored to Airtable: %s", counts)
        return counts

    # ── 3. Full pipeline ──────────────────────────────────────────────────

    def run_full_pipeline(self, report_date: str | None = None) -> dict[str, Any]:
        """Pull → normalize → store → return compiled data."""
        raw = self.pull_all_sources(report_date)
        self.normalize_and_store(raw)
        return raw

    # ── Individual pullers ────────────────────────────────────────────────

    def _pull_singular_campaigns(self, report_date: str) -> list[CampaignMetric]:
        from app.services.singular import SingularClient
        self._singular_client = SingularClient()
        loop = _get_or_create_loop()
        return loop.run_until_complete(
            self._singular_client.pull_campaign_data(report_date, report_date)
        )

    def _pull_singular_creatives(self, report_date: str) -> list[CreativeMetric]:
        from app.services.singular import SingularClient
        client = SingularClient()
        loop = _get_or_create_loop()
        return loop.run_until_complete(
            client.pull_creative_data(report_date, report_date)
        )

    @staticmethod
    def _pull_meta(report_date: str) -> list[CampaignMetric]:
        from app.services.meta_ads import MetaAdsClient
        client = MetaAdsClient()
        if not client._access_token:
            return []
        loop = _get_or_create_loop()
        rows = loop.run_until_complete(
            client.get_campaign_insights(report_date, report_date)
        )
        return [client.normalize_campaign(r) for r in rows]

    @staticmethod
    def _pull_applovin(report_date: str) -> list[CampaignMetric]:
        from app.services.applovin import AppLovinClient
        client = AppLovinClient()
        if not client._api_key:
            return []
        loop = _get_or_create_loop()
        rows = loop.run_until_complete(
            client.get_campaign_report(report_date, report_date)
        )
        return [client.normalize_campaign(r) for r in rows]

    @staticmethod
    def _pull_google(report_date: str) -> list[CampaignMetric]:
        from app.services.google_ads import GoogleAdsClient
        client = GoogleAdsClient()
        if not client._developer_token:
            return []
        loop = _get_or_create_loop()
        rows = loop.run_until_complete(
            client.get_campaign_performance(report_date, report_date)
        )
        return [client.normalize_campaign(r) for r in rows]

    @staticmethod
    def _pull_max(report_date: str) -> list[WaterfallInstance]:
        from app.services.max_reporting import MaxReportingClient
        client = MaxReportingClient()
        if not client.api_key:
            return []
        loop = _get_or_create_loop()
        return loop.run_until_complete(
            client.pull_waterfall_data(report_date)
        )

    @staticmethod
    def _pull_gmail() -> list[dict]:
        from app.services.gmail import GmailService
        svc = GmailService()
        if svc._service is None:
            return []
        return svc.scan_inbox(hours=24)

    @staticmethod
    def _pull_slack() -> list[dict]:
        # Slack context is populated by the reaction listener;
        # return empty here as the pipeline doesn't directly read threads.
        return []

    # ── Store helper ──────────────────────────────────────────────────────

    @staticmethod
    def _store_result(results: dict, name: str, data: Any) -> None:
        """Route pulled data into the appropriate results key."""
        key_map = {
            "Singular campaigns": "campaign_data",
            "Singular creatives": "creative_data",
            "Meta campaigns": "meta_campaigns",
            "AppLovin campaigns": "applovin_campaigns",
            "Google Ads": "google_campaigns",
            "MAX mediation": "waterfall_data",
            "Gmail inbox": "email_insights",
            "Slack context": "slack_context",
        }
        key = key_map.get(name)
        if key and isinstance(data, list):
            results[key] = data


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_or_create_loop() -> asyncio.AbstractEventLoop:
    """Get the running event loop or create a new one."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError
        return loop
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop
