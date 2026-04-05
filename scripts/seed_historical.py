"""
Historical data seeder.

Backfills the Airtable base with 30 days of historical campaign data
from Singular to bootstrap anomaly detection baselines.

Takes ~5 minutes to run due to Singular async report generation.

Run:
    python -m scripts.seed_historical      (from project root)
    python scripts/seed_historical.py      (direct)
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import date, timedelta

sys.path.insert(0, ".")

from app.config import get_settings  # noqa: E402


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
    )
    logger = logging.getLogger(__name__)

    cfg = get_settings()
    if not cfg.singular_api_key:
        print("ERROR: SINGULAR_API_KEY not set in .env")
        sys.exit(1)

    if not cfg.airtable_api_key or not cfg.airtable_base_id:
        print("ERROR: AIRTABLE_API_KEY and AIRTABLE_BASE_ID required")
        sys.exit(1)

    days = 30
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=days - 1)

    print(f"Seeding {days} days of historical data: {start} → {end}")
    print()

    # ── Pull from Singular ────────────────────────────────────────────────
    from app.services.singular import SingularClient

    async def pull() -> tuple:
        client = SingularClient()
        print("Pulling campaign data from Singular (this may take a few minutes)…")
        campaigns = await client.pull_campaign_data(start.isoformat(), end.isoformat())
        print(f"  Campaign rows: {len(campaigns)}")

        print("Pulling creative data…")
        creatives = await client.pull_creative_data(start.isoformat(), end.isoformat())
        print(f"  Creative rows: {len(creatives)}")

        if not client.revenue_data_available:
            print("  ⚠ Revenue data returned $0 (known Singular gap)")

        return campaigns, creatives

    campaigns, creatives = asyncio.run(pull())

    if not campaigns:
        print("No campaign data returned — check SINGULAR_API_KEY and date range")
        sys.exit(1)

    # ── Store in Airtable ─────────────────────────────────────────────────
    from app.services.airtable import AirtableService

    print()
    print("Upserting to Airtable…")

    airtable = AirtableService()

    # Campaign metrics in batches
    print(f"  Writing {len(campaigns)} campaign rows…")
    written = airtable.upsert_daily_metrics(campaigns)
    print(f"  ✓ {written} campaign rows upserted")

    # Creative metrics
    if creatives:
        print(f"  Writing {len(creatives)} creative rows…")
        written = airtable.upsert_creative_performance(creatives)
        print(f"  ✓ {written} creative rows upserted")

    # ── Summary ───────────────────────────────────────────────────────────
    networks = {r.network for r in campaigns}
    dates = {r.date for r in campaigns if r.date}
    date_range = f"{min(dates)} → {max(dates)}" if dates else "N/A"

    print()
    print("=" * 50)
    print("  Seeding complete!")
    print(f"  Date range: {date_range}")
    print(f"  Networks: {', '.join(sorted(networks))}")
    print(f"  Campaign rows: {len(campaigns)}")
    print(f"  Creative rows: {len(creatives)}")
    print("=" * 50)


if __name__ == "__main__":
    main()
