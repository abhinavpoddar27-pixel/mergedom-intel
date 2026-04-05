"""
Google Ads API client.

Retrieves campaign and ad-level reporting data via the Google Ads REST
API using OAuth2 credentials and a developer token.  Supplements Singular
with real-time data, billing alerts, and ad disapproval notifications.

Auth: OAuth2 access token (auto-refreshed from refresh token) +
      developer token header.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime

import httpx
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from app.config import get_settings
from app.models.schemas import CampaignMetric, CreativeMetric

logger = logging.getLogger(__name__)

_BASE_URL = "https://googleads.googleapis.com/v17"
_REQUEST_TIMEOUT = 30
_MAX_RETRIES = 3
_RETRY_BACKOFF = 2
_RETRY_STATUS_CODES = {429, 500, 502, 503}

_CAMPAIGN_QUERY = """
    SELECT
        campaign.id,
        campaign.name,
        metrics.cost_micros,
        metrics.impressions,
        metrics.clicks,
        metrics.conversions,
        metrics.cost_per_conversion,
        segments.date
    FROM campaign
    WHERE segments.date BETWEEN '{start}' AND '{end}'
    ORDER BY metrics.cost_micros DESC
"""

_AD_QUERY = """
    SELECT
        ad_group_ad.ad.id,
        ad_group_ad.ad.name,
        campaign.id,
        campaign.name,
        metrics.cost_micros,
        metrics.impressions,
        metrics.clicks,
        metrics.conversions,
        segments.date
    FROM ad_group_ad
    WHERE segments.date BETWEEN '{start}' AND '{end}'
    ORDER BY metrics.cost_micros DESC
"""


class GoogleAdsClient:
    """Client for the Google Ads REST API."""

    BASE_URL = _BASE_URL

    def __init__(self) -> None:
        cfg = get_settings()
        self._developer_token = cfg.google_ads_developer_token
        self._client_id = cfg.google_ads_client_id
        self._client_secret = cfg.google_ads_client_secret
        self._refresh_token = cfg.google_ads_refresh_token
        self._customer_id = cfg.google_ads_customer_id
        self._access_token: str | None = None

    # ── Auth ──────────────────────────────────────────────────────────────

    def _refresh_access_token(self) -> str:
        """Exchange refresh token for a fresh access token."""
        creds = Credentials(
            token=None,
            refresh_token=self._refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=self._client_id,
            client_secret=self._client_secret,
        )
        creds.refresh(Request())
        self._access_token = creds.token
        return self._access_token

    def _headers(self) -> dict[str, str]:
        if not self._access_token:
            self._refresh_access_token()
        return {
            "Authorization": f"Bearer {self._access_token}",
            "developer-token": self._developer_token,
        }

    # ── HTTP helper ───────────────────────────────────────────────────────

    async def _search(self, query: str) -> list[dict]:
        """Execute a GAQL search query and return result rows."""
        customer = self._customer_id.replace("-", "")
        url = f"{_BASE_URL}/customers/{customer}/googleAds:searchStream"
        last_exc: Exception | None = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                    logger.debug("Google Ads query (attempt %d)", attempt)
                    resp = await client.post(
                        url,
                        headers=self._headers(),
                        json={"query": query},
                    )

                    if resp.status_code == 401:
                        self._refresh_access_token()
                        continue

                    if resp.status_code in _RETRY_STATUS_CODES:
                        wait = _RETRY_BACKOFF ** attempt
                        logger.warning("Google Ads %d — retry in %ds", resp.status_code, wait)
                        await asyncio.sleep(wait)
                        continue

                    resp.raise_for_status()
                    data = resp.json()

                    # searchStream returns a list of batches
                    rows: list[dict] = []
                    for batch in data if isinstance(data, list) else [data]:
                        rows.extend(batch.get("results", []))
                    return rows

            except httpx.TimeoutException as exc:
                last_exc = exc
                await asyncio.sleep(_RETRY_BACKOFF ** attempt)
            except httpx.HTTPStatusError as exc:
                raise GoogleAdsError(
                    f"HTTP {exc.response.status_code}: {exc.response.text}"
                ) from exc

        raise GoogleAdsError(f"Google Ads API failed after {_MAX_RETRIES} retries") from last_exc

    # ── Public API ────────────────────────────────────────────────────────

    async def get_campaign_performance(
        self, start_date: str, end_date: str
    ) -> list[dict]:
        """Pull campaign-level performance metrics."""
        logger.info("Pulling Google Ads campaigns %s → %s", start_date, end_date)
        query = _CAMPAIGN_QUERY.format(start=start_date, end=end_date)
        return await self._search(query)

    async def get_ad_performance(
        self, start_date: str, end_date: str
    ) -> list[dict]:
        """Pull ad-level performance metrics."""
        logger.info("Pulling Google Ads ad performance %s → %s", start_date, end_date)
        query = _AD_QUERY.format(start=start_date, end=end_date)
        return await self._search(query)

    async def get_billing_info(self) -> dict:
        """Fetch billing/account budget information."""
        customer = self._customer_id.replace("-", "")
        url = f"{_BASE_URL}/customers/{customer}/billingSetups"
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            resp = await client.get(url, headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    # ── Normalizers ───────────────────────────────────────────────────────

    @staticmethod
    def normalize_campaign(row: dict) -> CampaignMetric:
        campaign = row.get("campaign", {})
        metrics = row.get("metrics", {})
        segments = row.get("segments", {})

        cost_micros = _int(metrics.get("costMicros", 0))
        spend = cost_micros / 1_000_000
        impressions = _int(metrics.get("impressions"))
        clicks = _int(metrics.get("clicks"))
        conversions = _int(float(metrics.get("conversions", 0)))
        cpi = spend / conversions if conversions > 0 else 0.0
        ipm = conversions / impressions * 1000 if impressions > 0 else 0.0
        ctr = clicks / impressions * 100 if impressions > 0 else 0.0

        return CampaignMetric(
            date=_parse_date(segments.get("date")),
            network="google",
            campaign_id=str(campaign.get("id", "")),
            campaign_name=str(campaign.get("name", "")),
            spend=round(spend, 2),
            installs=conversions,
            impressions=impressions,
            clicks=clicks,
            cpi=round(cpi, 4),
            ipm=round(ipm, 2),
            ctr=round(ctr, 4),
        )

    @staticmethod
    def normalize_ad(row: dict) -> CreativeMetric:
        ad = row.get("adGroupAd", {}).get("ad", {})
        metrics = row.get("metrics", {})

        cost_micros = _int(metrics.get("costMicros", 0))
        spend = cost_micros / 1_000_000
        impressions = _int(metrics.get("impressions"))
        clicks = _int(metrics.get("clicks"))
        conversions = _int(float(metrics.get("conversions", 0)))
        ipm = conversions / impressions * 1000 if impressions > 0 else 0.0
        ctr = clicks / impressions * 100 if impressions > 0 else 0.0

        return CreativeMetric(
            creative_id=str(ad.get("id", "")),
            concept_tag=str(ad.get("name", "")),
            platform="google",
            ipm=round(ipm, 2),
            ctr=round(ctr, 4),
            spend=round(spend, 2),
            impressions=impressions,
            installs=conversions,
        )


class GoogleAdsError(Exception):
    """Raised when a Google Ads API call fails."""


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
