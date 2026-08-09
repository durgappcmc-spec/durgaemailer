# NOTE: APP_NAME is a constant so the product can be renamed without hunting string literals.
# Load .env from this file's directory (not cwd) with override=True so OS env
# leftovers cannot pin an obsolete GEMINI_MODEL like gemini-2.0-flash.
# On Streamlit Community Cloud, secrets from the dashboard are mirrored into os.environ.
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

APP_NAME = "Relay"

_ROOT = Path(__file__).resolve().parent
load_dotenv(_ROOT / ".env", override=True)


def _apply_streamlit_secrets() -> None:
    """Copy Streamlit Cloud / local secrets.toml keys into os.environ."""
    try:
        import streamlit as st  # type: ignore

        secrets = st.secrets  # type: ignore[attr-defined]
    except Exception:
        return

    keys = (
        "GEMINI_API_KEY",
        "GEMINI_MODEL",
        "APOLLO_API_KEY",
        "ZOOMINFO_USERNAME",
        "ZOOMINFO_PASSWORD",
        "ZOOMINFO_API_KEY",
        "ZOOMINFO_CLIENT_ID",
        "ZOOMINFO_PRIVATE_KEY_PATH",
        "ROCKETREACH_API_KEY",
        "GOOGLE_SHEET_ID",
        "TRACKING_BASE_URL",
        "APPS_SCRIPT_TRACKING_URL",
        "APP_USERNAME",
        "APP_PASSWORD",
        "GMAIL_TOKEN_JSON",
        "GMAIL_OAUTH_JSON",
        "GMAIL_CLIENT_SECRETS",
        "GMAIL_TOKEN_PATH",
        "GMAIL_FROM_EMAIL",
        "GMAIL_DEFAULT_CC",
        "CHROMA_DIR",
        "AUTO_SYNC_GMAIL",
        "AUTO_SYNC_INTERVAL_MINUTES",
        "AUTO_SYNC_GMAIL_DAYS",
        "AUTO_SYNC_MAX_PER",
        "AUTO_INGEST_PROSPECTS",
    )
    for key in keys:
        try:
            if key in secrets and secrets[key] not in (None, ""):
                if not os.environ.get(key):
                    os.environ[key] = str(secrets[key])
        except Exception:
            continue


_apply_streamlit_secrets()

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
        self.ZOOMINFO_API_KEY: str = os.getenv("ZOOMINFO_API_KEY", "")
        self.ZOOMINFO_CLIENT_ID: str = os.getenv("ZOOMINFO_CLIENT_ID", "")
        self.ZOOMINFO_PRIVATE_KEY_PATH: str = os.getenv(
            "ZOOMINFO_PRIVATE_KEY_PATH", "./credentials/zoominfo.txt"
        )
        self.ROCKETREACH_API_KEY: str = os.getenv("ROCKETREACH_API_KEY", "")
        self.GMAIL_CLIENT_SECRETS: str = os.getenv(
            "GMAIL_CLIENT_SECRETS", "./credentials/gmail_oauth.json"
        )
        self.GMAIL_TOKEN_PATH: str = os.getenv(
            "GMAIL_TOKEN_PATH", "./credentials/gmail_token.json"
        )
        # Optional: paste full token JSON into Streamlit secrets for cloud deploys
        self.GMAIL_TOKEN_JSON: str = os.getenv("GMAIL_TOKEN_JSON", "")
        self.GMAIL_OAUTH_JSON: str = os.getenv("GMAIL_OAUTH_JSON", "")
        self.GMAIL_FROM_EMAIL: str = os.getenv(
            "GMAIL_FROM_EMAIL", "csr@karunamedia.org"
        )
        self.GMAIL_DEFAULT_CC: str = os.getenv("GMAIL_DEFAULT_CC", "")
        self.GOOGLE_SHEET_ID: str = os.getenv("GOOGLE_SHEET_ID", "")
        self.TRACKING_BASE_URL: str = os.getenv("TRACKING_BASE_URL", "").rstrip("/")
        self.APPS_SCRIPT_TRACKING_URL: str = os.getenv("APPS_SCRIPT_TRACKING_URL", "")
        self.CHROMA_DIR: str = str(_CHROMA)
        self.APP_USERNAME: str = os.getenv("APP_USERNAME", "")
        self.APP_PASSWORD: str = os.getenv("APP_PASSWORD", "")
        # Auto-sync: Gmail → memory + ZoomInfo/prospect searches → memory
        self.AUTO_SYNC_GMAIL: str = os.getenv("AUTO_SYNC_GMAIL", "true")
        self.AUTO_SYNC_INTERVAL_MINUTES: int = int(
            os.getenv("AUTO_SYNC_INTERVAL_MINUTES", "30") or 30
        )
        self.AUTO_SYNC_GMAIL_DAYS: int = int(os.getenv("AUTO_SYNC_GMAIL_DAYS", "30") or 30)
        self.AUTO_SYNC_MAX_PER: int = int(os.getenv("AUTO_SYNC_MAX_PER", "75") or 75)
        self.AUTO_INGEST_PROSPECTS: str = os.getenv("AUTO_INGEST_PROSPECTS", "true")


settings = Settings()

# Ensure local storage dirs exist on import.
_DATA.mkdir(parents=True, exist_ok=True)
Path(settings.CHROMA_DIR).mkdir(parents=True, exist_ok=True)
(_ROOT / "credentials").mkdir(parents=True, exist_ok=True)

# Materialize OAuth JSON from secrets when files are absent (Streamlit Cloud).
if settings.GMAIL_OAUTH_JSON:
    oauth_path = Path(settings.GMAIL_CLIENT_SECRETS)
    if not oauth_path.exists():
        oauth_path.parent.mkdir(parents=True, exist_ok=True)
        oauth_path.write_text(settings.GMAIL_OAUTH_JSON, encoding="utf-8")
if settings.GMAIL_TOKEN_JSON:
    token_path = Path(settings.GMAIL_TOKEN_PATH)
    if not token_path.exists():
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(settings.GMAIL_TOKEN_JSON, encoding="utf-8")
