"""
Meta Marketing API client.

Retrieves campaign and creative-level insights from the Meta (Facebook)
Graph API.  Supplements Singular data with creative thumbnails, real-time
spend, and richer creative breakdowns.

API: Graph API v18.0
Auth: access_token query parameter
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime

import httpx

from app.config import get_settings
from app.models.schemas import CampaignMetric, CreativeMetric

logger = logging.getLogger(__name__)

_BASE_URL = "https://graph.facebook.com/v18.0"
_REQUEST_TIMEOUT = 30
_MAX_RETRIES = 3
_RETRY_BACKOFF = 2
_RETRY_STATUS_CODES = {429, 500, 502, 503}

_CAMPAIGN_FIELDS = (
    "campaign_id,campaign_name,spend,impressions,clicks,"
    "actions,cost_per_action_type,cpm,ctr,reach,frequency"
)
_CREATIVE_FIELDS = (
    "ad_id,ad_name,campaign_id,campaign_name,"
    "spend,impressions,clicks,actions,ctr,cpm,"
    "creative{thumbnail_url}"
)


class MetaAdsClient:
    """Client for the Meta Marketing API."""

    BASE_URL = _BASE_URL

    def __init__(self) -> None:
        cfg = get_settings()
        self._access_token = cfg.meta_access_token
        self._ad_account_id = cfg.meta_ad_account_id

    # ── HTTP helper ───────────────────────────────────────────────────────

    async def _request(self, url: str, params: dict | None = None) -> dict:
        merged = {"access_token": self._access_token, **(params or {})}
        last_exc: Exception | None = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                    logger.debug("Meta API GET %s (attempt %d)", url, attempt)
                    resp = await client.get(url, params=merged)

                    if resp.status_code in _RETRY_STATUS_CODES:
                        wait = _RETRY_BACKOFF ** attempt
                        logger.warning("Meta API %d — retry in %ds", resp.status_code, wait)
                        await asyncio.sleep(wait)
                        continue

                    resp.raise_for_status()
                    return resp.json()
            except httpx.TimeoutException as exc:
                last_exc = exc
                await asyncio.sleep(_RETRY_BACKOFF ** attempt)
            except httpx.HTTPStatusError as exc:
                raise MetaAdsError(f"HTTP {exc.response.status_code}: {exc.response.text}") from exc

        raise MetaAdsError(f"Meta API failed after {_MAX_RETRIES} retries") from last_exc

    # ── Pagination ────────────────────────────────────────────────────────

    async def _paginate_results(self, url: str, params: dict | None = None) -> list[dict]:
        all_rows: list[dict] = []
        data = await self._request(url, params)
        all_rows.extend(data.get("data", []))

        while True:
            paging = data.get("paging", {})
            next_url = paging.get("next")
            if not next_url:
                break
            data = await self._request(next_url)
            all_rows.extend(data.get("data", []))

        return all_rows

    # ── Public API ────────────────────────────────────────────────────────

    async def get_ad_account_info(self) -> dict:
        """Fetch basic ad account metadata."""
        url = f"{_BASE_URL}/act_{self._ad_account_id}"
        return await self._request(url, {"fields": "name,currency,timezone_name,account_status"})

    async def get_campaign_insights(
        self, start_date: str, end_date: str, fields: str = _CAMPAIGN_FIELDS
    ) -> list[dict]:
        """Pull campaign-level insights for the given date range."""
        url = f"{_BASE_URL}/act_{self._ad_account_id}/insights"
        return await self._paginate_results(url, {
            "fields": fields,
            "time_range": f'{{"since":"{start_date}","until":"{end_date}"}}',
            "level": "campaign",
            "time_increment": 1,
            "limit": 500,
        })

    async def get_creative_insights(
        self, start_date: str, end_date: str, fields: str = _CREATIVE_FIELDS
    ) -> list[dict]:
        """Pull ad-level insights with creative thumbnail data."""
        url = f"{_BASE_URL}/act_{self._ad_account_id}/insights"
        return await self._paginate_results(url, {
            "fields": fields,
            "time_range": f'{{"since":"{start_date}","until":"{end_date}"}}',
            "level": "ad",
            "time_increment": 1,
            "limit": 500,
        })

    # ── Normalizers ───────────────────────────────────────────────────────

    @staticmethod
    def normalize_campaign(row: dict) -> CampaignMetric:
        spend = _float(row.get("spend"))
        impressions = _int(row.get("impressions"))
        clicks = _int(row.get("clicks"))
        installs = _extract_installs(row.get("actions", []))
        cpi = spend / installs if installs > 0 else 0.0
        ipm = installs / impressions * 1000 if impressions > 0 else 0.0
        ctr = clicks / impressions * 100 if impressions > 0 else 0.0

        return CampaignMetric(
            date=_parse_date(row.get("date_start")),
            network="meta",
            campaign_id=str(row.get("campaign_id", "")),
            campaign_name=str(row.get("campaign_name", "")),
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
        installs = _extract_installs(row.get("actions", []))
        ipm = installs / impressions * 1000 if impressions > 0 else 0.0
        ctr = clicks / impressions * 100 if impressions > 0 else 0.0

        return CreativeMetric(
            creative_id=str(row.get("ad_id", "")),
            concept_tag=str(row.get("ad_name", "")),
            platform="meta",
            ipm=round(ipm, 2),
            ctr=round(ctr, 4),
            spend=round(spend, 2),
            impressions=impressions,
            installs=installs,
        )


class MetaAdsError(Exception):
    """Raised when a Meta Ads API call fails."""


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

def _extract_installs(actions: list[dict]) -> int:
    """Extract install count from Meta's actions array."""
    for a in (actions or []):
        if a.get("action_type") in ("mobile_app_install", "app_install", "omni_app_install"):
            return _int(a.get("value"))
    return 0
