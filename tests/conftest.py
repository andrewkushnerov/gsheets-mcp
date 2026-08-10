import pytest

from gsheets_mcp import google_client
from gsheets_mcp.config import get_settings


@pytest.fixture(autouse=True)
def clean_settings():
    """Settings are cached for the process; drop the cache around every test."""
    get_settings.cache_clear()
    google_client.reset_service()
    yield
    get_settings.cache_clear()
    google_client.reset_service()


@pytest.fixture
def env(monkeypatch):
    """Set env vars and re-read settings."""

    def _set(**values):
        for key, value in values.items():
            monkeypatch.setenv(key.upper(), str(value))
        get_settings.cache_clear()

    return _set
