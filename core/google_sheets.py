# NOTE: Shared Sheets OAuth for durable store + tracking seed (BOOTSTRAP_TOKEN_*).
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

from config import settings

_ROOT = Path(__file__).resolve().parents[1]
_SVC = None
_SVC_AT = 0.0
_CREDS = None


def sheets_credentials():
    global _CREDS
    if _CREDS is not None and getattr(_CREDS, "valid", False):
        return _CREDS
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError:
        return None

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.file",
    ]
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
        _CREDS = creds
        return creds
    except Exception as e:
        print(f"[sheets] creds failed: {e}", file=sys.stderr)
        return None


def sheets_service():
    """Cached Sheets client (refresh on expiry)."""
    global _SVC, _SVC_AT
    now = time.time()
    if _SVC is not None and (now - _SVC_AT) < 300:
        return _SVC
    creds = sheets_credentials()
    if not creds:
        return None
    try:
        from googleapiclient.discovery import build

        _SVC = build("sheets", "v4", credentials=creds, cache_discovery=False)
        _SVC_AT = now
        return _SVC
    except Exception as e:
        print(f"[sheets] build failed: {e}", file=sys.stderr)
        return None


def sheet_id() -> str:
    return (settings.GOOGLE_SHEET_ID or "").strip()


_TAB_OK: set[str] = set()


def ensure_tab(title: str, headers: list[str]) -> bool:
    """Create a sheet tab with header row if missing. Cached per process."""
    if title in _TAB_OK:
        return True
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
        else:
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
        _TAB_OK.add(title)
        return True
    except Exception as e:
        print(f"[sheets] ensure_tab {title} failed: {e}", file=sys.stderr)
        return False
