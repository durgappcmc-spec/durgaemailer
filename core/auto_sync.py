# NOTE: Quiet background sync — Gmail + contacts into memory without manual clicks.
from __future__ import annotations

import json
import sys
import time
from typing import Any, Optional

from config import _DATA, settings
from connectors.ingest_to_memory import ingest_prospects
from core import memory
from gmail_client.extract import contacts_from_mailbox, extract_inbox_and_sent

_STATE_PATH = _DATA / "auto_sync_state.json"
_last_run_at: float = 0.0
_last_messages: list[dict[str, Any]] = []


def _env_bool(name: str, default: bool = True) -> bool:
    raw = str(getattr(settings, name, "") or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _load_state() -> dict[str, Any]:
    try:
        if _STATE_PATH.exists():
            return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_state(state: dict[str, Any]) -> None:
    try:
        _DATA.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[auto_sync] state save error: {e}", file=sys.stderr)


def ingest_mailbox_messages(messages: list[dict[str, Any]]) -> dict[str, int]:
    """Save raw emails + derived contacts into memory. Idempotent via upsert ids."""
    email_ids: list[str] = []
    for r in messages or []:
        mid = str(r.get("message_id") or "").strip()
        if not mid:
            continue
        text = json.dumps(
            {
                "subject": r.get("subject"),
                "from": r.get("from"),
                "to": r.get("to"),
                "date": r.get("date"),
                "mailbox": r.get("mailbox"),
                "body": (r.get("body_text") or "")[:800],
                "extracted": r.get("extracted") or {},
            },
            default=str,
        )
        added = memory.add(
            texts=text,
            source="gmail_extract",
            source_id=mid,
            title=r.get("subject") or "email",
            metadata={
                "from": r.get("from") or "",
                "to": r.get("to") or "",
                "mailbox": r.get("mailbox") or "",
            },
        )
        email_ids.extend(added)

    contacts = contacts_from_mailbox(messages, prefer="auto")
    prospects: list[dict[str, Any]] = []
    from connectors import sanitize_prospect

    for c in contacts:
        prospects.append(
            sanitize_prospect(
                {
                    "name": c.get("name") or "",
                    "first_name": c.get("first_name") or "",
                    "last_name": "",
                    "email": c.get("email") or "",
                    "title": c.get("title") or "",
                    "company": c.get("company") or "",
                    "source": "gmail",
                    "source_id": (c.get("email") or "").lower(),
                    "phone": "",
                    "linkedin_url": "",
                    "location": "",
                    "seniority": "",
                    "department": "",
                }
            )
        )
    contact_ids = ingest_prospects(prospects, source_tag="gmail_contacts")
    return {
        "emails": len(email_ids),
        "contacts": len(contact_ids),
        "messages": len(messages or []),
    }


def sync_gmail(
    *,
    days: Optional[int] = None,
    max_per: Optional[int] = None,
    force: bool = False,
) -> dict[str, Any]:
    """Pull inbox+sent and upsert into memory. Rate-limited unless force=True."""
    global _last_run_at, _last_messages
    if not _env_bool("AUTO_SYNC_GMAIL", True):
        return {"skipped": True, "reason": "AUTO_SYNC_GMAIL disabled"}

    interval_min = int(getattr(settings, "AUTO_SYNC_INTERVAL_MINUTES", 30) or 30)
    now = time.time()
    state = _load_state()
    last = float(state.get("gmail_last_sync_at") or _last_run_at or 0)
    if not force and last and (now - last) < max(60, interval_min * 60):
        return {
            "skipped": True,
            "reason": "recently synced",
            "seconds_ago": int(now - last),
            "emails": int(state.get("gmail_emails") or 0),
            "contacts": int(state.get("gmail_contacts") or 0),
            "messages": int(state.get("gmail_messages") or 0),
            "mailbox": list(_last_messages),
        }

    days_n = int(
        days if days is not None else getattr(settings, "AUTO_SYNC_GMAIL_DAYS", 30) or 30
    )
    max_n = int(
        max_per
        if max_per is not None
        else getattr(settings, "AUTO_SYNC_MAX_PER", 75) or 75
    )

    try:
        messages = extract_inbox_and_sent(
            days=days_n,
            max_per_mailbox=max_n,
            ai_extract=False,
            include_inbox=True,
            include_sent=True,
        )
    except Exception as e:
        print(f"[auto_sync] gmail pull error: {e}", file=sys.stderr)
        return {"ok": False, "error": str(e)}

    counts = ingest_mailbox_messages(messages)
    _last_run_at = now
    _last_messages = list(messages)
    state.update(
        {
            "gmail_last_sync_at": now,
            "gmail_emails": counts["emails"],
            "gmail_contacts": counts["contacts"],
            "gmail_messages": counts["messages"],
            "gmail_days": days_n,
        }
    )
    _save_state(state)
    return {"ok": True, **counts, "days": days_n, "mailbox": messages}


def ensure_session_sync(session_state: Any, *, light: bool = False) -> dict[str, Any]:
    """Run once per Streamlit session. Chat uses light=True (skip blocking Gmail)."""
    if not session_state.get("_memory_hydrated"):
        session_state["_memory_hydrated"] = True
        try:
            memory.hydrate_from_cloud()
        except Exception as e:
            print(f"[auto_sync] memory hydrate: {e}", file=sys.stderr)

    # Render wipes local disk each deploy — restore contacts before any save
    if not session_state.get("_prospects_drive_hydrated"):
        session_state["_prospects_drive_hydrated"] = True
        try:
            from core.prospect_list import reload_from_drive

            n = reload_from_drive()
            session_state["_prospects_restored_n"] = n
            print(f"[auto_sync] restored {n} prospects from Drive", file=sys.stderr)
        except Exception as e:
            print(f"[auto_sync] prospect hydrate: {e}", file=sys.stderr)

    if light:
        # Chat page: never block on Gmail
        return session_state.get("_auto_sync_result") or {"skipped": True, "reason": "light"}

    if session_state.get("_auto_sync_done"):
        return session_state.get("_auto_sync_result") or {"skipped": True}

    result = sync_gmail(force=False)
    session_state["_auto_sync_done"] = True
    session_state["_auto_sync_result"] = {
        k: v for k, v in result.items() if k != "mailbox"
    }
    mailbox = result.get("mailbox") or []
    if mailbox and not session_state.get("last_mailbox"):
        session_state["last_mailbox"] = mailbox
    return result


def auto_ingest_prospects(prospects: list[dict[str, Any]]) -> list[str]:
    """Save ZoomInfo / provider search hits into memory when enabled."""
    if not _env_bool("AUTO_INGEST_PROSPECTS", True):
        return []
    return ingest_prospects(prospects, source_tag="prospects")
