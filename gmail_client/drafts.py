# NOTE: List / fetch / send Gmail drafts (with HTML body decode).
from __future__ import annotations

import base64
import re
import sys
from email import message_from_bytes
from typing import Any, Optional

from gmail_client.auth import gmail_service

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def list_gmail_drafts(limit: int = 50) -> list[dict[str, Any]]:
    """Return lightweight Gmail draft rows (id, to, subject, snippet, updated)."""
    try:
        svc = gmail_service()
        res = (
            svc.users()
            .drafts()
            .list(userId="me", maxResults=min(max(int(limit), 1), 100))
            .execute()
        )
    except Exception as e:
        print(f"[gmail] list drafts failed: {e}", file=sys.stderr)
        return [{"error": str(e)}]

    out: list[dict[str, Any]] = []
    for row in res.get("drafts") or []:
        did = row.get("id") or ""
        msg = row.get("message") or {}
        mid = msg.get("id") or ""
        # list payload is thin — fetch metadata for headers when possible
        meta = _draft_headers(did) if did else {}
        out.append(
            {
                "draft_id": f"gmail:{did}",
                "gmail_draft_id": did,
                "gmail_message_id": mid or meta.get("message_id") or "",
                "recipient": meta.get("to") or "",
                "to": meta.get("to") or "",
                "cc": meta.get("cc") or "",
                "subject": meta.get("subject") or "(no subject)",
                "snippet": meta.get("snippet") or msg.get("snippet") or "",
                "updated_at": meta.get("internal_date") or "",
                "status": "draft",
                "source": "gmail",
                "tracking_id": meta.get("tracking_id") or "",
                "has_open_pixel": bool(meta.get("has_open_pixel")),
            }
        )
    return out


def get_gmail_draft(gmail_draft_id: str) -> dict[str, Any]:
    """Full draft with decoded HTML body."""
    did = (gmail_draft_id or "").removeprefix("gmail:")
    try:
        svc = gmail_service()
        full = (
            svc.users()
            .drafts()
            .get(userId="me", id=did, format="full")
            .execute()
        )
    except Exception as e:
        return {"error": str(e), "gmail_draft_id": did}

    msg = full.get("message") or {}
    headers = {
        h["name"].lower(): h["value"]
        for h in (msg.get("payload") or {}).get("headers") or []
    }
    html, text = _extract_bodies(msg.get("payload") or {})
    body_html = html or (f"<pre>{text}</pre>" if text else "")
    tracking_id = _extract_tracking_id(body_html)
    to = headers.get("to") or ""
    to_email = (_EMAIL_RE.findall(to) or [to])[0] if to else ""
    return {
        "draft_id": f"gmail:{did}",
        "gmail_draft_id": did,
        "gmail_message_id": msg.get("id") or "",
        "to": to_email,
        "recipient": to_email or to,
        "cc": headers.get("cc") or "",
        "subject": headers.get("subject") or "(no subject)",
        "body_html": body_html,
        "snippet": msg.get("snippet") or "",
        "status": "draft",
        "source": "gmail",
        "tracking_id": tracking_id or "",
        "has_open_pixel": bool(tracking_id)
        or "/.netlify/functions/open" in (body_html or "")
        or "/t/o/" in (body_html or ""),
        "from": headers.get("from") or "",
    }


def send_gmail_draft(gmail_draft_id: str) -> dict[str, Any]:
    """Send an existing Gmail draft (preserves MIME + open pixel already in body)."""
    did = (gmail_draft_id or "").removeprefix("gmail:")
    # Ensure tracking pixel present before send
    draft = get_gmail_draft(did)
    if draft.get("error"):
        return draft
    body = draft.get("body_html") or ""
    tid = draft.get("tracking_id") or ""
    if not draft.get("has_open_pixel"):
        try:
            from core.tracking import inject_tracking

            body, tid = inject_tracking(
                body,
                tracking_id=tid or None,
                recipient_email=draft.get("to") or "",
                subject=draft.get("subject") or "",
                register=True,
            )
            # Update draft MIME then send
            _update_draft_html(did, draft, body)
            draft["body_html"] = body
            draft["tracking_id"] = tid
            draft["has_open_pixel"] = True
        except Exception as e:
            print(f"[gmail] tracking inject before send failed: {e}", file=sys.stderr)

    try:
        svc = gmail_service()
        sent = (
            svc.users()
            .drafts()
            .send(userId="me", body={"id": did})
            .execute()
        )
        return {
            "ok": True,
            "message_id": sent.get("id"),
            "thread_id": sent.get("threadId"),
            "tracking_id": tid,
            "tracked": bool(tid),
            "gmail_draft_id": did,
            "to": draft.get("to"),
            "subject": draft.get("subject"),
        }
    except Exception as e:
        print(f"[gmail] send draft failed: {e}", file=sys.stderr)
        return {"error": str(e), "gmail_draft_id": did, "tracking_id": tid}


def _draft_headers(gmail_draft_id: str) -> dict[str, Any]:
    try:
        svc = gmail_service()
        full = (
            svc.users()
            .drafts()
            .get(userId="me", id=gmail_draft_id, format="full")
            .execute()
        )
    except Exception:
        return {}
    msg = full.get("message") or {}
    headers = {
        h["name"].lower(): h["value"]
        for h in (msg.get("payload") or {}).get("headers") or []
    }
    html, _text = _extract_bodies(msg.get("payload") or {})
    tid = _extract_tracking_id(html or "")
    to = headers.get("to") or ""
    to_email = (_EMAIL_RE.findall(to) or [to])[0] if to else ""
    internal = msg.get("internalDate")
    updated = ""
    if internal:
        try:
            from datetime import datetime, timezone

            updated = datetime.fromtimestamp(
                int(internal) / 1000.0, tz=timezone.utc
            ).isoformat().replace("+00:00", "Z")
        except Exception:
            updated = str(internal)
    return {
        "to": to_email,
        "cc": headers.get("cc") or "",
        "subject": headers.get("subject") or "",
        "snippet": msg.get("snippet") or "",
        "message_id": msg.get("id") or "",
        "internal_date": updated,
        "tracking_id": tid or "",
        "has_open_pixel": bool(tid)
        or "/.netlify/functions/open" in (html or "")
        or "/t/o/" in (html or ""),
    }


def _extract_bodies(payload: dict) -> tuple[str, str]:
    html = ""
    text = ""

    def walk(part: dict) -> None:
        nonlocal html, text
        mime = (part.get("mimeType") or "").lower()
        body = part.get("body") or {}
        data = body.get("data")
        if data and mime in ("text/html", "text/plain"):
            try:
                raw = base64.urlsafe_b64decode(data.encode("utf-8"))
                decoded = raw.decode("utf-8", errors="replace")
            except Exception:
                decoded = ""
            if mime == "text/html" and not html:
                html = decoded
            elif mime == "text/plain" and not text:
                text = decoded
        for child in part.get("parts") or []:
            walk(child)

    walk(payload or {})
    return html, text


def _extract_tracking_id(html: str) -> Optional[str]:
    if not html:
        return None
    try:
        from core.tracking import extract_tracking_id

        return extract_tracking_id(html)
    except Exception:
        m = re.search(
            r"(?:/\.netlify/functions/open|/t/o/)(?:\?id=)?([0-9a-fA-F\-]{8,})",
            html,
        )
        return m.group(1) if m else None


def _update_draft_html(gmail_draft_id: str, draft: dict, body_html: str) -> None:
    """Replace draft MIME with tracked HTML (best-effort)."""
    from gmail_client.send import _build_raw_message

    raw, _tid = _build_raw_message(
        to=draft.get("to") or "",
        subject=draft.get("subject") or "",
        html_body=body_html,
        instrument=False,
        include_signature=False,
        from_email=(draft.get("from") or None),
        cc=draft.get("cc") or None,
    )
    svc = gmail_service()
    svc.users().drafts().update(
        userId="me",
        id=gmail_draft_id,
        body={"message": {"raw": raw}},
    ).execute()
