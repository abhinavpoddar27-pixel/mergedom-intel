"""
Connection tester.

Verifies every configured API credential by making a lightweight test
call to each external service.  Prints a status line for each:
CONNECTED / FAILED (with detail) / SKIPPED (no key configured).

Run:
    python -m scripts.test_connections     (from project root)
    python scripts/test_connections.py     (direct)
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, ".")

from app.config import get_settings  # noqa: E402

logger = logging.getLogger(__name__)

# ── Status helpers ───────────────────────────────────────────────────────────

_GREEN = "\033[92m"
_RED = "\033[91m"
_YELLOW = "\033[93m"
_RESET = "\033[0m"

_results: list[tuple[str, str, str]] = []


def _connected(name: str, detail: str = "") -> None:
    msg = f"  {_GREEN}CONNECTED{_RESET}  {name}"
    if detail:
        msg += f"  — {detail}"
    print(msg)
    _results.append((name, "CONNECTED", detail))


def _failed(name: str, error: str) -> None:
    msg = f"  {_RED}FAILED{_RESET}     {name}  — {error}"
    print(msg)
    _results.append((name, "FAILED", error))


def _skipped(name: str) -> None:
    msg = f"  {_YELLOW}SKIPPED{_RESET}    {name}  — no API key configured"
    print(msg)
    _results.append((name, "SKIPPED", ""))


# ── Individual tests ─────────────────────────────────────────────────────────

def test_singular() -> None:
    """1. Singular: check data availability."""
    cfg = get_settings()
    if not cfg.singular_api_key:
        _skipped("Singular")
        return
    try:
        from app.services.singular import SingularClient
        client = SingularClient()
        result = asyncio.get_event_loop().run_until_complete(
            client.check_data_availability(date.today().isoformat())
        )
        _connected("Singular", f"data_availability responded")
    except Exception as exc:
        _failed("Singular", str(exc)[:120])


def test_meta() -> None:
    """2. Meta: get ad account info."""
    cfg = get_settings()
    if not cfg.meta_access_token:
        _skipped("Meta Ads")
        return
    try:
        from app.services.meta_ads import MetaAdsClient
        client = MetaAdsClient()
        result = asyncio.get_event_loop().run_until_complete(
            client.get_ad_account_info()
        )
        name = result.get("name", "unknown")
        _connected("Meta Ads", f"account: {name}")
    except Exception as exc:
        _failed("Meta Ads", str(exc)[:120])


def test_applovin() -> None:
    """3. AppLovin: pull 1-day report."""
    cfg = get_settings()
    if not cfg.applovin_api_key:
        _skipped("AppLovin")
        return
    try:
        from app.services.applovin import AppLovinClient
        client = AppLovinClient()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        rows = asyncio.get_event_loop().run_until_complete(
            client.get_campaign_report(yesterday, yesterday)
        )
        _connected("AppLovin", f"{len(rows)} campaign rows")
    except Exception as exc:
        _failed("AppLovin", str(exc)[:120])


def test_google_ads() -> None:
    """4. Google Ads: get campaign list."""
    cfg = get_settings()
    if not cfg.google_ads_developer_token:
        _skipped("Google Ads")
        return
    try:
        from app.services.google_ads import GoogleAdsClient
        client = GoogleAdsClient()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        rows = asyncio.get_event_loop().run_until_complete(
            client.get_campaign_performance(yesterday, yesterday)
        )
        _connected("Google Ads", f"{len(rows)} campaign rows")
    except Exception as exc:
        _failed("Google Ads", str(exc)[:120])


def test_max_mediation() -> None:
    """5. MAX: get mediation networks."""
    cfg = get_settings()
    if not cfg.max_api_key:
        _skipped("MAX Mediation")
        return
    try:
        from app.services.max_reporting import MaxReportingClient
        client = MaxReportingClient()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        perf = asyncio.get_event_loop().run_until_complete(
            client.get_network_performance(yesterday, yesterday)
        )
        _connected("MAX Mediation", f"{len(perf)} networks")
    except Exception as exc:
        _failed("MAX Mediation", str(exc)[:120])


def test_slack() -> None:
    """6. Slack: verify auth."""
    cfg = get_settings()
    if not cfg.slack_bot_token:
        _skipped("Slack")
        return
    try:
        from slack_sdk import WebClient
        client = WebClient(token=cfg.slack_bot_token)
        auth = client.auth_test()
        user = auth.get("user", "unknown")
        _connected("Slack", f"bot user: @{user}")
    except Exception as exc:
        _failed("Slack", str(exc)[:120])


def test_gmail() -> None:
    """7. Gmail: list recent emails."""
    cfg = get_settings()
    if not cfg.gmail_refresh_token:
        _skipped("Gmail")
        return
    try:
        from app.services.gmail import GmailService
        svc = GmailService()
        if svc._service is None:
            _failed("Gmail", "service init failed")
            return
        profile = svc._service.users().getProfile(userId="me").execute()
        email = profile.get("emailAddress", "unknown")
        _connected("Gmail", f"authenticated as {email}")
    except Exception as exc:
        _failed("Gmail", str(exc)[:120])


def test_airtable() -> None:
    """8. Airtable: validate base connection."""
    cfg = get_settings()
    if not cfg.airtable_api_key or not cfg.airtable_base_id:
        _skipped("Airtable")
        return
    try:
        from pyairtable import Api
        api = Api(cfg.airtable_api_key)
        base = api.base(cfg.airtable_base_id, validate=True)
        schema = base.schema()
        tables = [t.name for t in schema.tables]
        _connected("Airtable", f"{len(tables)} tables in base")
    except Exception as exc:
        _failed("Airtable", str(exc)[:120])


def test_claude() -> None:
    """9. Claude: generate test completion."""
    cfg = get_settings()
    if not cfg.anthropic_api_key:
        _skipped("Claude / Anthropic")
        return
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
        resp = client.messages.create(
            model=cfg.claude.model_parse,
            max_tokens=10,
            messages=[{"role": "user", "content": "Say OK"}],
        )
        text = resp.content[0].text.strip()
        _connected("Claude / Anthropic", f"model={cfg.claude.model_parse} response='{text}'")
    except Exception as exc:
        _failed("Claude / Anthropic", str(exc)[:120])


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    print()
    print("=" * 60)
    print("  MERGEDOM INTEL — CONNECTION TEST")
    print("=" * 60)
    print()

    tests = [
        test_singular,
        test_meta,
        test_applovin,
        test_google_ads,
        test_max_mediation,
        test_slack,
        test_gmail,
        test_airtable,
        test_claude,
    ]

    for test_fn in tests:
        test_fn()

    # Summary
    connected = sum(1 for _, s, _ in _results if s == "CONNECTED")
    failed = sum(1 for _, s, _ in _results if s == "FAILED")
    skipped = sum(1 for _, s, _ in _results if s == "SKIPPED")

    print()
    print("=" * 60)
    print(f"  {_GREEN}{connected} connected{_RESET}  "
          f"{_RED}{failed} failed{_RESET}  "
          f"{_YELLOW}{skipped} skipped{_RESET}")
    print("=" * 60)
    print()

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
