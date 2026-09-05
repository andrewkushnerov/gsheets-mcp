"""Runtime configuration, read from environment variables (and an optional .env).

Everything has a default that works on localhost, so `python -m gsheets_mcp` runs
with no configuration at all beyond Google credentials.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
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
    # Wire format for the tools that return a grid of cells. The same 1000x8 table
    # costs ~37k tokens as indented JSON and ~16k as TSV, and the API hands us
    # every cell as a string anyway, so TSV loses nothing. Set "json" if something
    # downstream parses the tool output instead of reading it.
    gsheets_output_format: Literal["tsv", "json"] = "tsv"
    # Off by default: searching Drive is the one capability that lets the model
    # discover documents you never handed it. Turning it on also needs a wider
    # OAuth scope, so re-run scripts/google_authorize.py afterwards.
    gsheets_enable_drive_search: bool = False

    log_level: str = "INFO"

    @field_validator("gsheets_output_format", mode="before")
    @classmethod
    def _normalise_output_format(cls, value):
        """Accept ``TSV`` and `` tsv `` the same as ``tsv`` — it arrives from a shell."""
        return value.strip().lower() if isinstance(value, str) else value

    @property
    def allowed_spreadsheets(self) -> set[str]:
        return {s.strip() for s in self.gsheets_allowed_spreadsheets.split(",") if s.strip()}

    @property
    def auth_enabled(self) -> bool:
        return bool(self.mcp_auth_token)


@lru_cache
def get_settings() -> Settings:
    return Settings()
