"""
Gmail OAuth2 setup script.

Walks through the interactive OAuth2 authorization flow:
  1. Prints a URL for the user to visit and authorize.
  2. User pastes the authorization code back.
  3. Script exchanges it for a refresh token.
  4. Prints the refresh token to add to .env.
  5. Tests read access and draft creation.
  6. Creates the "UA Briefs" label if it doesn't exist.

Run:
    python -m scripts.setup_gmail          (from project root)
    python scripts/setup_gmail.py          (direct)
"""

from __future__ import annotations

import logging
import sys

sys.path.insert(0, ".")

from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402
from google.oauth2.credentials import Credentials  # noqa: E402
from google.auth.transport.requests import Request  # noqa: E402
from googleapiclient.discovery import build  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.services.gmail import SCOPES, UA_BRIEFS_LABEL  # noqa: E402

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
    )

    cfg = get_settings()
    client_id = cfg.gmail_client_id
    client_secret = cfg.gmail_client_secret
    refresh_token = cfg.gmail_refresh_token

    if not client_id or not client_secret:
        print("ERROR: Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in .env")
        print()
        print("To get these:")
        print("  1. Go to https://console.cloud.google.com/apis/credentials")
        print("  2. Create an OAuth 2.0 Client ID (type: Desktop app)")
        print("  3. Copy Client ID and Client Secret to .env")
        sys.exit(1)

    # ── Step 1: Check if we already have a refresh token ──────────────────
    if refresh_token:
        print("Refresh token found in .env — testing it …")
        try:
            creds = Credentials(
                token=None,
                refresh_token=refresh_token,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=client_id,
                client_secret=client_secret,
                scopes=SCOPES,
            )
            creds.refresh(Request())
            print("  Token is valid!")
            _run_tests(creds)
            return
        except Exception as exc:
            print(f"  Token refresh failed: {exc}")
            print("  Running authorization flow …")
            print()

    # ── Step 2: OAuth2 authorization flow ─────────────────────────────────
    print("=" * 60)
    print("GMAIL OAUTH2 AUTHORIZATION")
    print("=" * 60)
    print()

    # Build the OAuth flow using client_id and client_secret directly
    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)

    # Try local server first, fall back to console
    try:
        print("A browser window will open for authorization.")
        print("If it doesn't, use the URL printed below.")
        print()
        creds = flow.run_local_server(port=8090, open_browser=False)
    except Exception:
        print("Local server mode failed — using manual mode.")
        print()
        auth_url, _ = flow.authorization_url(prompt="consent")
        print(f"Visit this URL to authorize:\n\n  {auth_url}\n")
        code = input("Paste the authorization code here: ").strip()
        flow.fetch_token(code=code)
        creds = flow.credentials

    # ── Step 3: Print the refresh token ───────────────────────────────────
    print()
    print("=" * 60)
    print("SUCCESS! Add this to your .env file:")
    print("=" * 60)
    print()
    print(f"GMAIL_REFRESH_TOKEN={creds.refresh_token}")
    print()

    # ── Step 4: Run tests ─────────────────────────────────────────────────
    _run_tests(creds)


def _run_tests(creds: Credentials) -> None:
    """Test Gmail access: list messages, create/delete draft, create label."""
    service = build("gmail", "v1", credentials=creds)

    # ── Test 1: Profile ───────────────────────────────────────────────────
    print("\n--- Test 1: Profile ---")
    try:
        profile = service.users().getProfile(userId="me").execute()
        email = profile.get("emailAddress", "unknown")
        total = profile.get("messagesTotal", 0)
        print(f"  Authenticated as: {email}")
        print(f"  Total messages: {total:,}")
    except Exception as exc:
        print(f"  Profile test failed: {exc}")

    # ── Test 2: List recent emails ────────────────────────────────────────
    print("\n--- Test 2: Recent emails ---")
    try:
        results = (
            service.users()
            .messages()
            .list(userId="me", q="newer_than:24h", maxResults=5)
            .execute()
        )
        messages = results.get("messages", [])
        print(f"  Found {len(messages)} message(s) from the last 24h")
        for msg in messages[:3]:
            detail = (
                service.users()
                .messages()
                .get(userId="me", id=msg["id"], format="metadata",
                     metadataHeaders=["Subject", "From"])
                .execute()
            )
            headers = {
                h["name"]: h["value"]
                for h in detail.get("payload", {}).get("headers", [])
            }
            print(f"    From: {headers.get('From', '?')}")
            print(f"    Subject: {headers.get('Subject', '?')}")
    except Exception as exc:
        print(f"  List test failed: {exc}")

    # ── Test 3: Create and delete draft ───────────────────────────────────
    print("\n--- Test 3: Draft create/delete ---")
    try:
        import base64
        from email.mime.text import MIMEText

        msg = MIMEText("Test draft from Mergedom Intel setup — safe to delete.")
        msg["to"] = "test@example.com"
        msg["subject"] = "[TEST] Mergedom Intel Setup"
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

        draft = (
            service.users()
            .drafts()
            .create(userId="me", body={"message": {"raw": raw}})
            .execute()
        )
        draft_id = draft.get("id")
        print(f"  Created test draft: {draft_id}")

        service.users().drafts().delete(userId="me", id=draft_id).execute()
        print(f"  Deleted test draft: {draft_id}")
        print("  Draft permissions OK ✓")
    except Exception as exc:
        print(f"  Draft test failed: {exc}")
        print("  Check scopes: gmail.compose")

    # ── Test 4: UA Briefs label ───────────────────────────────────────────
    print(f"\n--- Test 4: '{UA_BRIEFS_LABEL}' label ---")
    try:
        results = service.users().labels().list(userId="me").execute()
        existing = {l["name"]: l["id"] for l in results.get("labels", [])}

        if UA_BRIEFS_LABEL in existing:
            print(f"  Label exists: {existing[UA_BRIEFS_LABEL]}")
        else:
            new_label = (
                service.users()
                .labels()
                .create(
                    userId="me",
                    body={
                        "name": UA_BRIEFS_LABEL,
                        "labelListVisibility": "labelShow",
                        "messageListVisibility": "show",
                    },
                )
                .execute()
            )
            print(f"  Created label: {new_label.get('id')}")
    except Exception as exc:
        print(f"  Label test failed: {exc}")

    print("\n" + "=" * 60)
    print("All tests complete!")


if __name__ == "__main__":
    main()
