# NOTE: APP_NAME is a constant so the product can be renamed without hunting string literals.
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

APP_NAME = "Relay"

load_dotenv()

_ROOT = Path(__file__).resolve().parent
_DATA = _ROOT / "data"
_CHROMA = Path(os.getenv("CHROMA_DIR", str(_DATA / "chroma")))


class Settings:
    """Runtime settings loaded from environment variables."""

    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    APOLLO_API_KEY: str = os.getenv("APOLLO_API_KEY", "")
    ZOOMINFO_USERNAME: str = os.getenv("ZOOMINFO_USERNAME", "")
    ZOOMINFO_PASSWORD: str = os.getenv("ZOOMINFO_PASSWORD", "")
    ROCKETREACH_API_KEY: str = os.getenv("ROCKETREACH_API_KEY", "")
    GMAIL_CLIENT_SECRETS: str = os.getenv(
        "GMAIL_CLIENT_SECRETS", "./credentials/gmail_oauth.json"
    )
    GMAIL_TOKEN_PATH: str = os.getenv(
        "GMAIL_TOKEN_PATH", "./credentials/gmail_token.json"
    )
    GOOGLE_SHEET_ID: str = os.getenv("GOOGLE_SHEET_ID", "")
    TRACKING_BASE_URL: str = os.getenv("TRACKING_BASE_URL", "").rstrip("/")
    APPS_SCRIPT_TRACKING_URL: str = os.getenv("APPS_SCRIPT_TRACKING_URL", "")
    CHROMA_DIR: str = str(_CHROMA)


settings = Settings()

# Ensure local storage dirs exist on import.
_DATA.mkdir(parents=True, exist_ok=True)
Path(settings.CHROMA_DIR).mkdir(parents=True, exist_ok=True)
Path("./credentials").mkdir(parents=True, exist_ok=True)
