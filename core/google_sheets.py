# NOTE: Shared Sheets OAuth for durable store + tracking seed (BOOTSTRAP_TOKEN_*).
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from config import settings

_ROOT = Path(__file__).resolve().parents[1]


def sheets_credentials():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError:
        return None

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    raw = (
        getattr(settings, "BOOTSTRAP_TOKEN_JSON", None)
        or os.getenv("BOOTSTRAP_TOKEN_JSON", "")
        or ""
    ).strip()
    path = Path(
        getattr(settings, "BOOTSTRAP_TOKEN_PATH", None)
        or os.getenv("BOOTSTRAP_TOKEN_PATH", str(_ROOT / "credentials" / "bootstrap_token.json"))
    )
    try:
        if raw:
            creds = Credentials.from_authorized_user_info(json.loads(raw), scopes)
        elif path.is_file() and path.stat().st_size > 0:
            creds = Credentials.from_authorized_user_file(str(path), scopes)
        else:
            return None
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return creds
    except Exception as e:
        print(f"[sheets] creds failed: {e}", file=sys.stderr)
        return None


def sheets_service():
    creds = sheets_credentials()
    if not creds:
        return None
    try:
        from googleapiclient.discovery import build

        return build("sheets", "v4", credentials=creds)
    except Exception as e:
        print(f"[sheets] build failed: {e}", file=sys.stderr)
        return None


def sheet_id() -> str:
    return (settings.GOOGLE_SHEET_ID or "").strip()


def ensure_tab(title: str, headers: list[str]) -> bool:
    """Create a sheet tab with header row if missing."""
    sid = sheet_id()
    svc = sheets_service()
    if not sid or not svc:
        return False
    try:
        meta = svc.spreadsheets().get(spreadsheetId=sid).execute()
        titles = {s["properties"]["title"] for s in meta.get("sheets") or []}
        if title not in titles:
            svc.spreadsheets().batchUpdate(
                spreadsheetId=sid,
                body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
            ).execute()
            svc.spreadsheets().values().update(
                spreadsheetId=sid,
                range=f"'{title}'!A1",
                valueInputOption="RAW",
                body={"values": [headers]},
            ).execute()
            return True
        # Ensure header exists
        vals = (
            svc.spreadsheets()
            .values()
            .get(spreadsheetId=sid, range=f"'{title}'!A1:C1")
            .execute()
            .get("values")
            or []
        )
        if not vals:
            svc.spreadsheets().values().update(
                spreadsheetId=sid,
                range=f"'{title}'!A1",
                valueInputOption="RAW",
                body={"values": [headers]},
            ).execute()
        return True
    except Exception as e:
        print(f"[sheets] ensure_tab {title} failed: {e}", file=sys.stderr)
        return False
