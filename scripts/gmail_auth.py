#!/usr/bin/env python3
"""
One-time OAuth setup for Gmail-based Uber receipt ingestion.

Prerequisites:
  1. Go to https://console.cloud.google.com → APIs & Services → Library →
     enable the "Gmail API".
  2. APIs & Services → Credentials → Create credentials → OAuth client ID.
     Application type: **Desktop app**. Download the JSON.
  3. Drop the downloaded file at  data/gmail/credentials.json  (relative to
     the project root). Don't commit it — it's already in .gitignore.

Then run from the project root:

    python3 scripts/gmail_auth.py

A browser window will open. Sign in to the Google account that receives your
Uber receipts and grant the requested scopes (read + label modify). The
script writes data/gmail/token.json with the refresh token. Re-run only if
the token is revoked or you change scopes.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import settings                                          # noqa: E402
from src.services.gmail_ingest import SCOPES                             # noqa: E402


def main() -> int:
    creds_path = settings.gmail_credentials_path
    token_path = settings.gmail_token_path

    if not creds_path.exists():
        print(f"Missing OAuth client file at: {creds_path}")
        print("Follow the setup steps at the top of this script.")
        return 1

    token_path.parent.mkdir(parents=True, exist_ok=True)

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
    # run_local_server picks a free port and opens the browser
    creds = flow.run_local_server(port=0)
    token_path.write_text(creds.to_json(), encoding="utf-8")

    print(f"\nToken saved to {token_path}.")
    print("You can now use the 'Uber via Gmail' card in the app to sync rides.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
