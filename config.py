# NOTE: APP_NAME is a constant so the product can be renamed without hunting string literals.
# Load .env from this file's directory (not cwd) with override=True so OS env
# leftovers cannot pin an obsolete GEMINI_MODEL like gemini-2.0-flash.
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

APP_NAME = "Relay"

_ROOT = Path(__file__).resolve().parent
load_dotenv(_ROOT / ".env", override=True)

_DATA = _ROOT / "data"
_CHROMA = Path(os.getenv("CHROMA_DIR", str(_DATA / "chroma")))


class Settings:
    """Runtime settings loaded from environment variables."""

    def __init__(self) -> None:
        self.GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
        self.GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
        self.APOLLO_API_KEY: str = os.getenv("APOLLO_API_KEY", "")
        self.ZOOMINFO_USERNAME: str = os.getenv("ZOOMINFO_USERNAME", "")
        self.ZOOMINFO_PASSWORD: str = os.getenv("ZOOMINFO_PASSWORD", "")
        self.ROCKETREACH_API_KEY: str = os.getenv("ROCKETREACH_API_KEY", "")
        self.GMAIL_CLIENT_SECRETS: str = os.getenv(
            "GMAIL_CLIENT_SECRETS", "./credentials/gmail_oauth.json"
        )
        self.GMAIL_TOKEN_PATH: str = os.getenv(
            "GMAIL_TOKEN_PATH", "./credentials/gmail_token.json"
        )
        self.GOOGLE_SHEET_ID: str = os.getenv("GOOGLE_SHEET_ID", "")
        self.TRACKING_BASE_URL: str = os.getenv("TRACKING_BASE_URL", "").rstrip("/")
        self.APPS_SCRIPT_TRACKING_URL: str = os.getenv("APPS_SCRIPT_TRACKING_URL", "")
        self.CHROMA_DIR: str = str(_CHROMA)


settings = Settings()

# Ensure local storage dirs exist on import.
_DATA.mkdir(parents=True, exist_ok=True)
Path(settings.CHROMA_DIR).mkdir(parents=True, exist_ok=True)
(_ROOT / "credentials").mkdir(parents=True, exist_ok=True)
