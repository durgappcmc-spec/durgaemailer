# NOTE: Direct Sheets writes when Apps Script web app is behind; never blocks sends.
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import settings

_ROOT = Path(__file__).resolve().parents[1]


def _sheets_creds():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError:
        return None

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    raw = os.getenv("BOOTSTRAP_TOKEN_JSON", "").strip()
    path = Path(os.getenv("BOOTSTRAP_TOKEN_PATH", str(_ROOT / "credentials" / "bootstrap_token.json")))
    try:
        if raw:
            info = json.loads(raw)
            creds = Credentials.from_authorized_user_info(info, scopes)
        elif path.is_file() and path.stat().st_size > 0:
            creds = Credentials.from_authorized_user_file(str(path), scopes)
        else:
            return None
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return creds
    except Exception as e:
        print(f"[tracking] sheets creds failed: {e}", file=sys.stderr)
        return None


def seed_send_row(
    *,
    email_id: str,
    recipient_email: str = "",
    recipient_name: str = "",
    subject: str = "",
    campaign: str = "",
    source: str = "relay_draft",
) -> bool:
    """Append a Sends row if email_id is not already present. Returns True on write."""
    if not email_id:
        return False
    sheet_id = (settings.GOOGLE_SHEET_ID or "").strip()
    if not sheet_id:
        return False
    creds = _sheets_creds()
    if not creds:
        return False
    try:
        from googleapiclient.discovery import build

        svc = build("sheets", "v4", credentials=creds)
        existing = (
            svc.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range="Sends!A:B")
            .execute()
            .get("values")
            or []
        )
        for row in existing[1:]:
            if len(row) > 1 and str(row[1]) == str(email_id):
                return False
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        svc.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range="Sends",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={
                "values": [
                    [
                        now,
                        email_id,
                        recipient_email or "",
                        recipient_name or "",
                        subject or "",
                        campaign or "",
                        source or "relay_draft",
                        "",
                        "",
                    ]
                ]
            },
        ).execute()
        return True
    except Exception as e:
        print(f"[tracking] seed_send_row failed: {e}", file=sys.stderr)
        return False
