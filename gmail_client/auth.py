# NOTE: First run opens a local browser for OAuth consent; token is cached thereafter.
# On Streamlit Community Cloud, provide GMAIL_TOKEN_JSON (+ GMAIL_OAUTH_JSON) via secrets.
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from config import settings

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]


def get_credentials() -> Credentials:
    """Load or refresh Gmail OAuth credentials."""
    token_path = Path(settings.GMAIL_TOKEN_PATH)
    secrets_path = Path(settings.GMAIL_CLIENT_SECRETS)
    creds: Credentials | None = None

    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except Exception as e:
            print(f"[gmail] failed loading token: {e}", file=sys.stderr)
            creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(creds.to_json(), encoding="utf-8")
            return creds
        except Exception as e:
            print(f"[gmail] token refresh failed: {e}", file=sys.stderr)

    if not secrets_path.exists():
        # Last chance: secret blob may have been set after Settings init
        oauth_blob = os.getenv("GMAIL_OAUTH_JSON", "")
        if oauth_blob:
            secrets_path.parent.mkdir(parents=True, exist_ok=True)
            secrets_path.write_text(oauth_blob, encoding="utf-8")
        else:
            raise FileNotFoundError(
                f"Gmail OAuth secrets not found at {secrets_path}. "
                "On Streamlit Cloud, set GMAIL_OAUTH_JSON + GMAIL_TOKEN_JSON in Secrets. "
                "Locally, download Desktop client JSON from Google Cloud Console."
            )

    # Headless / cloud: cannot open a local browser — require pre-baked token.
    if os.getenv("STREAMLIT_SERVER_HEADLESS", "").lower() in ("true", "1") or os.getenv(
        "STREAMLIT_RUNTIME_ENV"
    ) == "cloud":
        raise RuntimeError(
            "Gmail needs GMAIL_TOKEN_JSON in Streamlit secrets for cloud deploys "
            "(paste contents of credentials/gmail_token.json). "
            "Re-auth locally once if the token expired, then update the secret."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), SCOPES)
    creds = flow.run_local_server(port=0)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def gmail_service() -> Any:
    """Build and return a Gmail API service."""
    creds = get_credentials()
    return build("gmail", "v1", credentials=creds, cache_discovery=False)
