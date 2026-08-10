"""Builds the Google Sheets API client.

Two ways to authenticate, picked by configuration:

* **Service account** (``GOOGLE_SERVICE_ACCOUNT_FILE``) — best for a server. No
  browser, no refresh token to babysit. Share each spreadsheet with the service
  account's ``client_email`` as Editor.
* **Stored OAuth token** (``GOOGLE_TOKEN_FILE``) — the server acts as a real
  Google user, so it already sees everything that user owns. Create the token
  once with ``scripts/google_authorize.py``.

The interactive OAuth flow deliberately lives in that script and never here: a
server process has no browser, and a hidden "open a browser" call is exactly the
kind of thing that hangs in production at 3am.
"""
from __future__ import annotations

import json
import os
import threading

from .config import get_settings

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

#: Only asked for when Drive search is enabled. ``drive.metadata.readonly`` can
#: list names and ids and cannot read a single cell of file *content* — the
#: smallest scope that answers "which spreadsheet is called X?".
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.metadata.readonly"

_service = None
_drive_service = None
_lock = threading.Lock()


def required_scopes() -> list[str]:
    """Scopes this configuration needs. The authorize script asks for exactly these."""
    if get_settings().gsheets_enable_drive_search:
        return [*SCOPES, DRIVE_SCOPE]
    return list(SCOPES)


def _load_credentials():
    settings = get_settings()

    if settings.google_service_account_file:
        path = settings.google_service_account_file
        if not os.path.exists(path):
            raise RuntimeError(
                f"GOOGLE_SERVICE_ACCOUNT_FILE points at '{path}', which does not exist."
            )
        from google.oauth2 import service_account

        return service_account.Credentials.from_service_account_file(
            path, scopes=required_scopes()
        )

    token_path = settings.google_token_file
    if not os.path.exists(token_path):
        raise RuntimeError(
            f"No Google credentials. Either set GOOGLE_SERVICE_ACCOUNT_FILE, or run "
            f"`python scripts/google_authorize.py` once to create '{token_path}'."
        )

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials.from_authorized_user_file(token_path, required_scopes())
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(token_path, "w", encoding="utf-8") as fh:
                fh.write(creds.to_json())
        else:
            raise RuntimeError(
                f"The stored Google token '{token_path}' is invalid and cannot be refreshed. "
                "Delete it and run `python scripts/google_authorize.py` again."
            )
    return creds


def get_sheets_service():
    """Return a cached Sheets API service, building it on first use.

    Imports live inside the function so that starting the process — and running
    the tests — does not pay for googleapiclient's rather leisurely import.
    """
    global _service
    if _service is None:
        with _lock:
            if _service is None:
                from googleapiclient.discovery import build

                _service = build("sheets", "v4", credentials=_load_credentials(),
                                 cache_discovery=False)
    return _service


def get_drive_service():
    """Return a cached Drive API service, used only to search for spreadsheets.

    Checks the stored token's granted scopes first. Without that check the call
    still fails, but as a bare Google 403 several frames away from the cause —
    and the cause here is always the same thing: a token minted before Drive
    search was switched on.
    """
    global _drive_service
    settings = get_settings()
    if not settings.google_service_account_file:
        granted = _granted_scopes(settings.google_token_file)
        if granted is not None and DRIVE_SCOPE not in granted:
            raise RuntimeError(
                f"The stored Google token '{settings.google_token_file}' was granted without "
                f"the Drive scope, so it cannot search Drive. Delete it and run "
                "`python scripts/google_authorize.py` again with GSHEETS_ENABLE_DRIVE_SEARCH=true."
            )
    if _drive_service is None:
        with _lock:
            if _drive_service is None:
                from googleapiclient.discovery import build

                _drive_service = build("drive", "v3", credentials=_load_credentials(),
                                       cache_discovery=False)
    return _drive_service


def _granted_scopes(token_path: str) -> set[str] | None:
    """Scopes recorded in the token file, or None if it cannot say."""
    try:
        with open(token_path, encoding="utf-8") as fh:
            scopes = json.load(fh).get("scopes")
    except (OSError, ValueError):
        return None
    return set(scopes) if isinstance(scopes, list) else None


def service_account_email() -> str | None:
    """The service account's address, when that is how we authenticate.

    Read straight out of the key file rather than from the API: it costs nothing
    and the answer is only ever used to tell a human who owns a new file.
    """
    path = get_settings().google_service_account_file
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("client_email")
    except (OSError, ValueError):
        return None


def reset_service() -> None:
    """Drop the cached services (used by tests and after a credentials change)."""
    global _service, _drive_service
    with _lock:
        _service = None
        _drive_service = None
