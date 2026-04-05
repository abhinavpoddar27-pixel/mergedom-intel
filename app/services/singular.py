"""
Singular API client — primary data source.

Pulls campaign and creative performance data via Singular's async
reporting API.  The flow is: create report → poll status → download
results.

Known limitation: cohort revenue fields (revenue_1d / 7d / 30d)
currently return $0 from the Singular API.  When this is detected the
client logs a warning, falls back to CPI-based derived metrics, and
sets a ``revenue_data_available`` flag to ``False`` so downstream
consumers (daily brief, budget module) can surface the caveat.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime

import httpx

from app.config import get_settings
from app.models.schemas import CampaignMetric, CreativeMetric

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

_BASE_URL = "https://api.singular.net/api/v2.0"

_CAMPAIGN_DIMENSIONS = (
    "app,source,unified_campaign_id,unified_campaign_name,"
    "os,country_field"
)
_CREATIVE_DIMENSIONS = (
    "app,source,unified_campaign_id,unified_campaign_name,"
    "os,country_field,creative_id,creative_name,creative_image_url"
)
_METRICS = "adn_cost,adn_impressions,adn_clicks,adn_installs,tracker_installs"
_COHORT_METRICS = "revenue,roi"
_COHORT_PERIODS = "1d,7d,30d"

_POLL_INTERVAL_SECS = 11  # 10-12s recommended
_MAX_POLL_ATTEMPTS = 30
_REQUEST_TIMEOUT_SECS = 30
_TOTAL_TIMEOUT_SECS = 300  # 5 min hard ceiling

_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 2  # seconds, exponential


class SingularClient:
    """Async client for the Singular reporting API."""

    BASE_URL = _BASE_URL

    def __init__(self) -> None:
        cfg = get_settings()
        self.api_key = cfg.singular_api_key
        self.revenue_data_available: bool = True

    # ── Helpers ───────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self.api_key}

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict:
        """HTTP request with retry on transient errors."""
        last_exc: Exception | None = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECS) as client:
                    logger.debug(
                        "Singular API %s %s (attempt %d)", method, url, attempt
                    )
                    resp = await client.request(
                        method,
                        url,
                        headers=self._headers(),
                        params=params,
                        json=json_body,
                    )

                    if resp.status_code in _RETRY_STATUS_CODES:
                        wait = _RETRY_BACKOFF_BASE ** attempt
                        logger.warning(
                            "Singular returned %d — retrying in %ds",
                            resp.status_code,
                            wait,
                        )
                        await asyncio.sleep(wait)
                        continue

                    resp.raise_for_status()
                    return resp.json()

            except httpx.TimeoutException as exc:
                last_exc = exc
                logger.warning("Singular request timed out (attempt %d)", attempt)
                await asyncio.sleep(_RETRY_BACKOFF_BASE ** attempt)
            except httpx.HTTPStatusError as exc:
                # Non-retryable status already filtered above
                raise SingularAPIError(
                    f"HTTP {exc.response.status_code}: {exc.response.text}"
                ) from exc

        raise SingularAPIError(
            f"Singular request failed after {_MAX_RETRIES} retries"
        ) from last_exc

    # ── Public API ────────────────────────────────────────────────────────

    async def check_data_availability(self, date_str: str) -> dict:
        """Check whether Singular data is available for the given date.

        Returns the raw JSON response from the data_availability_status
        endpoint.
        """
        logger.info("Checking Singular data availability for %s", date_str)
        return await self._request(
            "GET",
            f"{_BASE_URL}/data_availability_status",
            params={"api_key": self.api_key, "format": "json"},
        )

    async def pull_campaign_data(
        self, start_date: str, end_date: str
    ) -> list[CampaignMetric]:
        """Pull campaign-level metrics for the given date range.

        Returns a list of ``CampaignMetric`` models with derived fields
        (CPI, IPM, CTR) computed client-side.
        """
        logger.info("Pulling Singular campaign data %s → %s", start_date, end_date)
        params = self._build_report_params(start_date, end_date, _CAMPAIGN_DIMENSIONS)
        raw_rows = await self._run_async_report(params)

        metrics: list[CampaignMetric] = []
        for row in raw_rows:
            metrics.append(self._normalize_campaign_row(row))
        logger.info("Normalised %d campaign rows from Singular", len(metrics))
        return metrics

    async def pull_creative_data(
        self, start_date: str, end_date: str
    ) -> list[CreativeMetric]:
        """Pull creative-level metrics for the given date range.

        Returns a list of ``CreativeMetric`` models.
        """
        logger.info("Pulling Singular creative data %s → %s", start_date, end_date)
        params = self._build_report_params(start_date, end_date, _CREATIVE_DIMENSIONS)
        raw_rows = await self._run_async_report(params)

        metrics: list[CreativeMetric] = []
        for row in raw_rows:
            metrics.append(self._normalize_creative_row(row))
        logger.info("Normalised %d creative rows from Singular", len(metrics))
        return metrics

    # ── Internal: async report lifecycle ──────────────────────────────────

    @staticmethod
    def _build_report_params(
        start_date: str, end_date: str, dimensions: str
    ) -> dict:
        return {
            "report_type": "combined",
            "format": "json",
            "start_date": start_date,
            "end_date": end_date,
            "dimensions": dimensions,
            "metrics": _METRICS,
            "cohort_metrics": _COHORT_METRICS,
            "cohort_periods": _COHORT_PERIODS,
            "time_breakdown": "day",
            "display_alignment": "false",
        }

    async def _run_async_report(self, params: dict) -> list[dict]:
        """Full create → poll → download cycle.  Returns raw result rows."""
        report_id = await self._create_report(params)
        return await self._poll_report(report_id)

    async def _create_report(self, params: dict) -> str:
        """POST to create_async_report and return the report_id."""
        data = await self._request(
            "POST",
            f"{_BASE_URL}/create_async_report",
            params={"api_key": self.api_key},
            json_body=params,
        )
        report_id = data.get("report_id", "")
        if not report_id:
            raise SingularAPIError(
                f"create_async_report did not return a report_id: {data}"
            )
        logger.info("Singular report created: %s", report_id)
        return report_id

    async def _poll_report(
        self, report_id: str, max_attempts: int = _MAX_POLL_ATTEMPTS
    ) -> list[dict]:
        """Poll get_report_status until DONE, then download and return rows."""
        deadline = time.monotonic() + _TOTAL_TIMEOUT_SECS

        for attempt in range(1, max_attempts + 1):
            if time.monotonic() > deadline:
                raise SingularAPIError(
                    f"Singular report {report_id} timed out after "
                    f"{_TOTAL_TIMEOUT_SECS}s"
                )

            data = await self._request(
                "GET",
                f"{_BASE_URL}/get_report_status",
                params={
                    "api_key": self.api_key,
                    "report_id": report_id,
                },
            )
            status = data.get("status", "").upper()
            logger.debug(
                "Report %s poll %d/%d — status=%s",
                report_id, attempt, max_attempts, status,
            )

            if status == "DONE":
                download_url = data.get("download_url") or data.get("url", "")
                if not download_url:
                    raise SingularAPIError(
                        f"Report DONE but no download URL: {data}"
                    )
                return await self._download_results(download_url)

            if status == "FAILED":
                raise SingularAPIError(
                    f"Singular report {report_id} failed: {data}"
                )

            await asyncio.sleep(_POLL_INTERVAL_SECS)

        raise SingularAPIError(
            f"Singular report {report_id} did not complete after "
            f"{max_attempts} polls"
        )

    async def _download_results(self, url: str) -> list[dict]:
        """Download the finished report JSON and return the results list."""
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECS) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            payload = resp.json()

        rows = payload.get("results", payload.get("value", []))
        count = payload.get("num_of_rows", len(rows))
        logger.info("Downloaded %d rows from Singular report", count)
        return rows

    # ── Row normalisers ───────────────────────────────────────────────────

    def _normalize_campaign_row(self, row: dict) -> CampaignMetric:
        """Map a raw Singular result row to a ``CampaignMetric``."""
        spend = _float(row.get("adn_cost"))
        impressions = _int(row.get("adn_impressions"))
        clicks = _int(row.get("adn_clicks"))
        installs = _int(
            row.get("adn_installs") or row.get("tracker_installs")
        )

        # Cohort revenue (known $0 issue)
        rev_1d = _float(row.get("revenue_1d") or row.get("cohort_revenue_1d"))
        rev_7d = _float(row.get("revenue_7d") or row.get("cohort_revenue_7d"))
        rev_30d = _float(row.get("revenue_30d") or row.get("cohort_revenue_30d"))

        all_rev_zero = rev_1d == 0.0 and rev_7d == 0.0 and rev_30d == 0.0
        if all_rev_zero and spend > 0:
            if self.revenue_data_available:
                logger.warning(
                    "Singular cohort revenue returned $0 — flagging "
                    "revenue_data_available=False"
                )
                self.revenue_data_available = False

        # Derived UA metrics
        cpi = (spend / installs) if installs > 0 else 0.0
        ipm = (installs / impressions * 1000) if impressions > 0 else 0.0
        ctr = (clicks / impressions * 100) if impressions > 0 else 0.0

        # ROAS (falls back to 0 when revenue is zero)
        roas_1d = (rev_1d / spend) if spend > 0 else 0.0
        roas_7d = (rev_7d / spend) if spend > 0 else 0.0
        roas_30d = (rev_30d / spend) if spend > 0 else 0.0

        return CampaignMetric(
            date=_parse_date(row.get("date")),
            network=str(row.get("source", "")),
            campaign_id=str(row.get("unified_campaign_id", "")),
            campaign_name=str(row.get("unified_campaign_name", "")),
            os=str(row.get("os", "")),
            country=str(row.get("country_field", "")),
            spend=round(spend, 2),
            installs=installs,
            impressions=impressions,
            clicks=clicks,
            cpi=round(cpi, 4),
            ipm=round(ipm, 2),
            ctr=round(ctr, 4),
            roas_d1=round(roas_1d, 4),
            roas_d7=round(roas_7d, 4),
            roas_d30=round(roas_30d, 4),
            revenue_d1=round(rev_1d, 2),
            revenue_d7=round(rev_7d, 2),
            revenue_d30=round(rev_30d, 2),
        )

    def _normalize_creative_row(self, row: dict) -> CreativeMetric:
        """Map a raw Singular result row to a ``CreativeMetric``."""
        spend = _float(row.get("adn_cost"))
        impressions = _int(row.get("adn_impressions"))
        clicks = _int(row.get("adn_clicks"))
        installs = _int(
            row.get("adn_installs") or row.get("tracker_installs")
        )
        rev_7d = _float(row.get("revenue_7d") or row.get("cohort_revenue_7d"))

        ipm = (installs / impressions * 1000) if impressions > 0 else 0.0
        ctr = (clicks / impressions * 100) if impressions > 0 else 0.0
        roas_7d = (rev_7d / spend) if spend > 0 else 0.0

        return CreativeMetric(
            creative_id=str(row.get("creative_id", "")),
            concept_tag=str(row.get("creative_name", "")),
            platform=str(row.get("source", "")),
            ipm=round(ipm, 2),
            ctr=round(ctr, 4),
            roas_d7=round(roas_7d, 4),
            spend=round(spend, 2),
            impressions=impressions,
            installs=installs,
            # fatigue_score, days_live, fatigue_curve_slope are computed
            # by the creative-fatigue module, not by the raw API pull.
        )


# ── Exceptions ───────────────────────────────────────────────────────────────

class SingularAPIError(Exception):
    """Raised when a Singular API call fails irrecoverably."""


# ── Private helpers ──────────────────────────────────────────────────────────

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
    if isinstance(val, date):
        return val
    try:
        return datetime.strptime(str(val), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


# ── Standalone connectivity test ─────────────────────────────────────────────

if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    async def _test() -> None:
        client = SingularClient()

        if not client.api_key:
            print("ERROR: SINGULAR_API_KEY is not set in .env")
            sys.exit(1)

        print(f"API key loaded ({len(client.api_key)} chars)")
        print("Checking data availability …")

        try:
            result = await client.check_data_availability(
                date.today().isoformat()
            )
            print("SUCCESS — Singular API responded:")
            print(json.dumps(result, indent=2, default=str))
        except SingularAPIError as exc:
            print(f"SINGULAR ERROR: {exc}")
            sys.exit(1)
        except Exception as exc:
            print(f"UNEXPECTED ERROR: {exc}")
            sys.exit(1)

    asyncio.run(_test())
