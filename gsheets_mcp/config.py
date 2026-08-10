"""Runtime configuration, read from environment variables (and an optional .env).

Everything has a default that works on localhost, so `python -m gsheets_mcp` runs
with no configuration at all beyond Google credentials.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- Google identity: service account OR stored OAuth token ---
    google_service_account_file: str = ""
    google_oauth_client_file: str = "credentials.json"
    google_token_file: str = "token.json"

    # --- HTTP transport ---
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8077
    mcp_auth_token: str = ""

    # --- Guard rails ---
    gsheets_read_only: bool = False
    # Comma-separated; kept as a plain string because pydantic-settings would try
    # to JSON-decode a list-typed field coming from the environment.
    gsheets_allowed_spreadsheets: str = ""
    gsheets_max_read_rows: int = 5000
    # Off by default: searching Drive is the one capability that lets the model
    # discover documents you never handed it. Turning it on also needs a wider
    # OAuth scope, so re-run scripts/google_authorize.py afterwards.
    gsheets_enable_drive_search: bool = False

    log_level: str = "INFO"

    @property
    def allowed_spreadsheets(self) -> set[str]:
        return {s.strip() for s in self.gsheets_allowed_spreadsheets.split(",") if s.strip()}

    @property
    def auth_enabled(self) -> bool:
        return bool(self.mcp_auth_token)


@lru_cache
def get_settings() -> Settings:
    return Settings()
