"""
MAX mediation reporting client.

Pulls ad-unit and network-level revenue data from the AppLovin MAX
reporting API for waterfall optimization analysis.

Networks: IronSource, Unity, AppLovin, TapJoy, AdJoy, FreeCash
Dimensions: network × geo_tier (T1/T2/T3) × ad_format
            (banner/interstitial/rewarded) × time_block (4-hour windows)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime

import httpx

from app.config import get_settings
from app.models.schemas import WaterfallInstance

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

_BASE_URL = "https://r.applovin.com/maxReport"
_REQUEST_TIMEOUT = 30
_MAX_RETRIES = 3
_RETRY_BACKOFF = 2
_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}

NETWORKS = [
    "IronSource", "Unity", "AppLovin", "TapJoy", "AdJoy", "FreeCash",
]
GEO_TIERS = ["T1", "T2", "T3"]
AD_FORMATS = ["banner", "interstitial", "rewarded"]
TIME_BLOCKS = ["0-4", "4-8", "8-12", "12-16", "16-20", "20-24"]


class MaxReportingClient:
    """Client for the AppLovin MAX mediation reporting API."""

    BASE_URL = _BASE_URL

    def __init__(self) -> None:
        cfg = get_settings()
        self.api_key = cfg.max_api_key

    # ── Helpers ───────────────────────────────────────────────────────────

    def _params(self) -> dict[str, str]:
        return {"api_key": self.api_key, "format": "json"}

    async def _request(
        self,
        params: dict | None = None,
    ) -> dict:
        """GET request with retry on transient errors."""
        merged = {**self._params(), **(params or {})}
        last_exc: Exception | None = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                    logger.debug("MAX API GET (attempt %d)", attempt)
                    resp = await client.get(_BASE_URL, params=merged)

                    if resp.status_code in _RETRY_STATUS_CODES:
                        wait = _RETRY_BACKOFF ** attempt
                        logger.warning(
                            "MAX API returned %d — retrying in %ds",
                            resp.status_code, wait,
                        )
                        await asyncio.sleep(wait)
                        continue

                    resp.raise_for_status()
                    return resp.json()

            except httpx.TimeoutException as exc:
                last_exc = exc
                logger.warning("MAX API timed out (attempt %d)", attempt)
                await asyncio.sleep(_RETRY_BACKOFF ** attempt)
            except httpx.HTTPStatusError as exc:
                raise MaxReportingError(
                    f"HTTP {exc.response.status_code}: {exc.response.text}"
                ) from exc

        raise MaxReportingError(
            f"MAX API request failed after {_MAX_RETRIES} retries"
        ) from last_exc

    # ── Public API ────────────────────────────────────────────────────────

    async def pull_waterfall_data(
        self, report_date: str
    ) -> list[WaterfallInstance]:
        """Pull waterfall-level data for a single date.

        Returns one ``WaterfallInstance`` per network × geo × format × time_block.
        """
        logger.info("Pulling MAX waterfall data for %s", report_date)
        data = await self._request({
            "start": report_date,
            "end": report_date,
            "columns": (
                "network,country_tier,ad_format,hour,"
                "ecpm,fill_rate,show_rate,estimated_revenue"
            ),
        })

        rows = data.get("results", data.get("rows", []))
        instances: list[WaterfallInstance] = []
        for row in rows:
            instances.append(self._normalize_row(row, report_date))

        logger.info("Normalised %d waterfall instances", len(instances))
        return instances

    async def get_network_performance(
        self, start_date: str, end_date: str
    ) -> dict[str, dict]:
        """Pull aggregated performance per network over a date range.

        Returns ``{network: {ecpm, fill_rate, revenue, impressions}}``.
        """
        logger.info("Pulling MAX network performance %s → %s", start_date, end_date)
        data = await self._request({
            "start": start_date,
            "end": end_date,
            "columns": "network,ecpm,fill_rate,estimated_revenue,impressions",
        })

        rows = data.get("results", data.get("rows", []))
        perf: dict[str, dict] = {}
        for row in rows:
            net = str(row.get("network", "unknown"))
            perf[net] = {
                "ecpm": _float(row.get("ecpm")),
                "fill_rate": _float(row.get("fill_rate")),
                "revenue": _float(row.get("estimated_revenue")),
                "impressions": _int(row.get("impressions")),
            }
        return perf

    async def get_floor_prices(self) -> dict[str, float]:
        """Return current floor prices per network.

        Uses the latest day's data and extracts floor prices from the
        ecpm/fill_rate relationship.  In practice, floor prices are
        managed in the MAX dashboard — this returns the effective floors
        observed in the data.
        """
        today = date.today().isoformat()
        instances = await self.pull_waterfall_data(today)

        floors: dict[str, list[float]] = {}
        for inst in instances:
            if inst.floor_price > 0:
                floors.setdefault(inst.network, []).append(inst.floor_price)

        return {
            net: round(sum(vals) / len(vals), 2) if vals else 0.0
            for net, vals in floors.items()
        }

    # ── Row normaliser ────────────────────────────────────────────────────

    @staticmethod
    def _normalize_row(row: dict, report_date: str) -> WaterfallInstance:
        """Map a raw MAX API row to a ``WaterfallInstance``."""
        hour = _int(row.get("hour", row.get("time_block", 0)))
        # Map hour to 4-hour block
        block_start = (hour // 4) * 4
        time_block = f"{block_start}-{block_start + 4}"

        return WaterfallInstance(
            network=str(row.get("network", "")),
            geo_tier=str(row.get("country_tier", row.get("geo_tier", ""))),
            ad_format=str(row.get("ad_format", "")),
            floor_price=_float(row.get("floor_price", row.get("ecpm_floor", 0))),
            ecpm=_float(row.get("ecpm")),
            fill_rate=_float(row.get("fill_rate")),
            show_rate=_float(row.get("show_rate")),
            revenue=_float(row.get("estimated_revenue", row.get("revenue", 0))),
            date=_parse_date(report_date),
            time_block=time_block,
        )


# ── Exceptions ───────────────────────────────────────────────────────────────

class MaxReportingError(Exception):
    """Raised when a MAX reporting API call fails."""


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
        return datetime.strptime(str(val)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


# ── Standalone test ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.DEBUG)

    async def _test() -> None:
        client = MaxReportingClient()
        if not client.api_key:
            print("ERROR: MAX_API_KEY not set in .env")
            sys.exit(1)
        print(f"API key loaded ({len(client.api_key)} chars)")
        try:
            perf = await client.get_network_performance(
                date.today().isoformat(), date.today().isoformat()
            )
            print("Network performance:")
            print(json.dumps(perf, indent=2, default=str))
        except MaxReportingError as exc:
            print(f"MAX API error: {exc}")
            sys.exit(1)

    asyncio.run(_test())
