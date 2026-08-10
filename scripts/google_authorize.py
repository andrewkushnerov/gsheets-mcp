#!/usr/bin/env python
"""One-time Google OAuth: turns credentials.json into token.json.

Run this on a machine with a browser:

    python scripts/google_authorize.py

It opens a Google consent screen, then writes the refresh token to
GOOGLE_TOKEN_FILE (default ./token.json). The server reads that file and refreshes
it silently from then on — copy it to your server and you are done.

Run it again after turning GSHEETS_ENABLE_DRIVE_SEARCH on: a token remembers the
scopes it was granted, and one minted without the Drive scope cannot search Drive.

Not needed if you authenticate with a service account.
"""
from __future__ import annotations

import os
import sys

# Nothing is installed, and running a script from scripts/ puts scripts/ on
# sys.path, not the repo root. Put the root there so gsheets_mcp is importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gsheets_mcp.config import get_settings
from gsheets_mcp.google_client import required_scopes


def main() -> int:
    settings = get_settings()
    client_file = settings.google_oauth_client_file
    token_file = settings.google_token_file
    scopes = required_scopes()

    if not os.path.exists(client_file):
        print(
            f"OAuth client file '{client_file}' not found.\n\n"
            "Get it from Google Cloud Console:\n"
            "  1. Create (or pick) a project, enable the Google Sheets API.\n"
            "  2. APIs & Services -> Credentials -> Create credentials -> OAuth client ID.\n"
            "  3. Application type: Desktop app.\n"
            f"  4. Download the JSON and save it as '{client_file}'.",
            file=sys.stderr,
        )
        return 1

    from google_auth_oauthlib.flow import InstalledAppFlow

    print("Requesting scopes:")
    for scope in scopes:
        print(f"  {scope}")

    flow = InstalledAppFlow.from_client_secrets_file(client_file, scopes)
    # port=0 lets the OS pick a free loopback port for the redirect.
    creds = flow.run_local_server(port=0)

    with open(token_file, "w", encoding="utf-8") as fh:
        fh.write(creds.to_json())
    os.chmod(token_file, 0o600)

    print(f"Saved credentials to {token_file}. Start the server with: python -m gsheets_mcp")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
