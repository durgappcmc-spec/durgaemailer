# NOTE: All calls go through Apps Script; failures are logged and returned as error dicts.
from __future__ import annotations

import sys
from datetime import datetime
from typing import Any, Optional

import requests

from config import settings


def _post(payload: dict[str, Any]) -> dict[str, Any]:
    url = settings.APPS_SCRIPT_TRACKING_URL
    if not url:
        msg = "APPS_SCRIPT_TRACKING_URL is not set"
        print(f"[apps_script] {msg}", file=sys.stderr)
        return {"ok": False, "error": msg}
    try:
        resp = requests.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {"ok": True, "raw": resp.text}
    except Exception as e:
        print(f"[apps_script] request failed: {e}", file=sys.stderr)
        return {"ok": False, "error": str(e)}


def schedule_email(
    recipient_email: str,
    subject: str,
    html_body: str,
    send_at: datetime | str,
    recipient_name: Optional[str] = None,
    campaign: Optional[str] = None,
    source: Optional[str] = None,
    attachments: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Schedule a single email via Apps Script."""
    if isinstance(send_at, datetime):
        send_at_iso = send_at.isoformat()
    else:
        send_at_iso = str(send_at)
    return _post(
        {
            "action": "schedule",
            "recipient_email": recipient_email,
            "recipient_name": recipient_name or "",
            "subject": subject,
            "html_body": html_body,
            "send_at": send_at_iso,
            "campaign": campaign or "",
            "source": source or "",
            "attachments": attachments or [],
        }
    )


def schedule_batch(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    """Schedule multiple emails. Each job matches schedule_email kwargs."""
    normalized = []
    for job in jobs:
        send_at = job.get("send_at")
        if isinstance(send_at, datetime):
            send_at = send_at.isoformat()
        normalized.append({**job, "send_at": send_at})
    return _post({"action": "schedule", "jobs": normalized})


def cancel_scheduled(
    recipient_email: Optional[str] = None,
    campaign: Optional[str] = None,
) -> dict[str, Any]:
    """Cancel pending scheduled emails filtered by recipient and/or campaign."""
    return _post(
        {
            "action": "cancel",
            "recipient_email": recipient_email or "",
            "campaign": campaign or "",
        }
    )


def list_scheduled(status: Optional[str] = None) -> dict[str, Any]:
    """List scheduled emails, optionally filtered by status."""
    return _post({"action": "list", "what": "scheduled", "status": status or ""})
