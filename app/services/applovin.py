"""
AppLovin Reporting API client.

Pulls campaign (advertiser) and monetization (publisher/MAX) reports.
Supplements Singular with real-time campaign data and provides the
MAX mediation waterfall data.

API: https://r.applovin.com/report
Auth: api_key query parameter
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime

import httpx

from app.config import get_settings
from app.models.schemas import CampaignMetric, CreativeMetric

logger = logging.getLogger(__name__)

_REPORT_URL = "https://r.applovin.com/report"
_MAX_REPORT_URL = "https://r.applovin.com/maxReport"
_REQUEST_TIMEOUT = 30
_MAX_RETRIES = 3
_RETRY_BACKOFF = 2
_RETRY_STATUS_CODES = {429, 500, 502, 503}


class AppLovinClient:
    """Client for the AppLovin reporting APIs."""

    BASE_URL = _REPORT_URL

    def __init__(self) -> None:
        cfg = get_settings()
        self._api_key = cfg.applovin_api_key

    # ── HTTP helper ───────────────────────────────────────────────────────

    async def _request(self, url: str, params: dict | None = None) -> dict:
        merged = {"api_key": self._api_key, "format": "json", **(params or {})}
        last_exc: Exception | None = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                    logger.debug("AppLovin API GET %s (attempt %d)", url, attempt)
                    resp = await client.get(url, params=merged)

                    if resp.status_code in _RETRY_STATUS_CODES:
                        wait = _RETRY_BACKOFF ** attempt
                        logger.warning("AppLovin %d — retry in %ds", resp.status_code, wait)
                        await asyncio.sleep(wait)
                        continue

                    resp.raise_for_status()
                    return resp.json()
            except httpx.TimeoutException as exc:
                last_exc = exc
                await asyncio.sleep(_RETRY_BACKOFF ** attempt)
            except httpx.HTTPStatusError as exc:
                raise AppLovinError(f"HTTP {exc.response.status_code}: {exc.response.text}") from exc

        raise AppLovinError(f"AppLovin API failed after {_MAX_RETRIES} retries") from last_exc

    # ── Public API ────────────────────────────────────────────────────────

    async def get_campaign_report(
        self, start_date: str, end_date: str
    ) -> list[dict]:
        """Pull advertiser campaign report."""
        logger.info("Pulling AppLovin campaign report %s → %s", start_date, end_date)
        data = await self._request(_REPORT_URL, {
            "start": start_date,
            "end": end_date,
            "report_type": "advertiser",
            "columns": (
                "day,campaign,campaign_id_external,"
                "impressions,clicks,installs,spend"
            ),
        })
        return data.get("results", data.get("rows", []))

    async def get_creative_report(
        self, start_date: str, end_date: str
    ) -> list[dict]:
        """Pull advertiser creative-level report."""
        logger.info("Pulling AppLovin creative report %s → %s", start_date, end_date)
        data = await self._request(_REPORT_URL, {
            "start": start_date,
            "end": end_date,
            "report_type": "advertiser",
            "columns": (
                "day,campaign,campaign_id_external,creative_id,"
                "impressions,clicks,installs,spend"
            ),
        })
        return data.get("results", data.get("rows", []))

    async def get_max_mediation_report(
        self, start_date: str, end_date: str
    ) -> list[dict]:
        """Pull MAX mediation (publisher) report."""
        logger.info("Pulling AppLovin MAX report %s → %s", start_date, end_date)
        data = await self._request(_MAX_REPORT_URL, {
            "start": start_date,
            "end": end_date,
            "columns": (
                "day,network,country,ad_format,"
                "impressions,ecpm,estimated_revenue,fill_rate,show_rate"
            ),
        })
        return data.get("results", data.get("rows", []))

    # ── Normalizers ───────────────────────────────────────────────────────

    @staticmethod
    def normalize_campaign(row: dict) -> CampaignMetric:
        spend = _float(row.get("spend"))
        impressions = _int(row.get("impressions"))
        clicks = _int(row.get("clicks"))
        installs = _int(row.get("installs"))
        cpi = spend / installs if installs > 0 else 0.0
        ipm = installs / impressions * 1000 if impressions > 0 else 0.0
        ctr = clicks / impressions * 100 if impressions > 0 else 0.0

        return CampaignMetric(
            date=_parse_date(row.get("day")),
            network="applovin",
            campaign_id=str(row.get("campaign_id_external", row.get("campaign_id", ""))),
            campaign_name=str(row.get("campaign", "")),
            spend=round(spend, 2),
            installs=installs,
            impressions=impressions,
            clicks=clicks,
            cpi=round(cpi, 4),
            ipm=round(ipm, 2),
            ctr=round(ctr, 4),
        )

    @staticmethod
    def normalize_creative(row: dict) -> CreativeMetric:
        spend = _float(row.get("spend"))
        impressions = _int(row.get("impressions"))
        clicks = _int(row.get("clicks"))
        installs = _int(row.get("installs"))
        ipm = installs / impressions * 1000 if impressions > 0 else 0.0
        ctr = clicks / impressions * 100 if impressions > 0 else 0.0

        return CreativeMetric(
            creative_id=str(row.get("creative_id", "")),
            concept_tag=str(row.get("campaign", "")),
            platform="applovin",
            ipm=round(ipm, 2),
            ctr=round(ctr, 4),
            spend=round(spend, 2),
            impressions=impressions,
            installs=installs,
        )


class AppLovinError(Exception):
    """Raised when an AppLovin API call fails."""


def _float(val) -> float:
    try:
        return float(val) if val is not None else 0.0
    except (ValueError, TypeError):
        return 0.0

def _int(val) -> int:
    try:
        return int(float(val)) if val is not None else 0
    except (ValueError, TypeError):
        return 0

def _parse_date(val) -> date | None:
    if val is None:
        return None
    try:
        return datetime.strptime(str(val)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
