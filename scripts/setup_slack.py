"""
Slack setup script.

Verifies bot permissions, lists existing channels, checks which
required channels exist, and tests the bot's ability to post and
delete messages.

Run:
    python -m scripts.setup_slack          (from project root)
    python scripts/setup_slack.py          (direct)
"""

from __future__ import annotations

import logging
import sys
import time

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

sys.path.insert(0, ".")

from app.config import get_settings  # noqa: E402
from app.services.slack_bot import CHANNELS  # noqa: E402

logger = logging.getLogger(__name__)

REQUIRED_CHANNELS = list(CHANNELS.values())


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
    )

    cfg = get_settings()
    bot_token = cfg.slack_bot_token
    app_token = cfg.slack_app_token

    if not bot_token:
        print("ERROR: Set SLACK_BOT_TOKEN in .env")
        sys.exit(1)

    client = WebClient(token=bot_token)

    # ── 1. Test auth ──────────────────────────────────────────────────────
    print("Testing bot authentication …")
    try:
        auth = client.auth_test()
        bot_user = auth.get("user", "unknown")
        team = auth.get("team", "unknown")
        print(f"  Authenticated as @{bot_user} in workspace '{team}'")
    except SlackApiError as exc:
        print(f"  Auth failed: {exc}")
        sys.exit(1)

    # ── 2. List channels ──────────────────────────────────────────────────
    print("\nFetching channel list …")
    try:
        result = client.conversations_list(
            types="public_channel,private_channel", limit=200
        )
        existing = {f"#{c['name']}" for c in result.get("channels", [])}
        print(f"  Found {len(existing)} channels")
    except SlackApiError as exc:
        print(f"  Could not list channels: {exc}")
        existing = set()

    # ── 3. Check required channels ────────────────────────────────────────
    print("\nChecking required channels:")
    missing = []
    for ch in REQUIRED_CHANNELS:
        if ch in existing:
            print(f"  ✓ {ch}")
        else:
            print(f"  ✗ {ch}  (missing)")
            missing.append(ch)

    if missing:
        print(f"\n{len(missing)} channel(s) need to be created.")
        print("Create them in Slack, then re-run this script.")
        print("\nSlack channel creation steps:")
        for ch in missing:
            name = ch.lstrip("#")
            print(f"  1. Click '+' next to Channels in Slack sidebar")
            print(f"  2. Name: {name}")
            print(f"  3. Set to public (or private if preferred)")
            print(f"  4. Invite the bot user (@{bot_user})")
            print()

    # ── 4. Test post + delete ─────────────────────────────────────────────
    # Pick the first available required channel to test in
    test_channel = None
    for ch in REQUIRED_CHANNELS:
        if ch in existing:
            test_channel = ch
            break

    if test_channel:
        print(f"\nTesting post + delete in {test_channel} …")
        try:
            resp = client.chat_postMessage(
                channel=test_channel,
                text=":white_check_mark: Mergedom Intel bot test — this message will be deleted.",
            )
            ts = resp.get("ts")
            ch_id = resp.get("channel")
            print(f"  Posted test message (ts={ts})")

            time.sleep(1)

            client.chat_delete(channel=ch_id, ts=ts)
            print("  Deleted test message")
            print("  Bot has post + delete permissions ✓")
        except SlackApiError as exc:
            print(f"  Post/delete test failed: {exc}")
            print("  Check bot scopes: chat:write, chat:write.public")
    else:
        print("\nSkipping post test — no required channels found.")

    # ── 5. Check socket mode ──────────────────────────────────────────────
    print(f"\nSocket Mode app token: {'set' if app_token else 'MISSING'}")
    if not app_token:
        print("  Socket Mode is required for reaction listening.")
        print("  Set SLACK_APP_TOKEN (xapp-...) in .env")

    # ── 6. Check reaction scopes ──────────────────────────────────────────
    print("\nRequired bot scopes:")
    scopes = [
        "chat:write", "chat:write.public", "channels:history",
        "channels:read", "groups:read", "reactions:read",
        "im:write", "users:read",
    ]
    for scope in scopes:
        print(f"  • {scope}")
    print("  Verify these in https://api.slack.com/apps → OAuth & Permissions")

    # ── Summary ───────────────────────────────────────────────────────────
    print("\n" + "=" * 50)
    if not missing and app_token:
        print("Setup looks good! All channels exist, tokens are set.")
    else:
        issues = []
        if missing:
            issues.append(f"{len(missing)} missing channel(s)")
        if not app_token:
            issues.append("missing SLACK_APP_TOKEN")
        print(f"Issues to resolve: {', '.join(issues)}")


if __name__ == "__main__":
    main()
