# NOTE: Raw MIME parse; HTML-only messages get text derived via BeautifulSoup.
from __future__ import annotations

import base64
import email
import json
import re
import sys
from email.message import Message
from typing import Any, Optional

from bs4 import BeautifulSoup

from core.llm import extract_json
from gmail_client.auth import gmail_service


def list_messages(query: str, max_results: int = 20) -> list[dict[str, str]]:
    """List Gmail message ids matching a search query (paginated)."""
    try:
        svc = gmail_service()
        out: list[dict[str, str]] = []
        page_token: Optional[str] = None
        remaining = max(1, int(max_results))
        while remaining > 0:
            page_size = min(500, remaining)
            kwargs: dict[str, Any] = {
                "userId": "me",
                "q": query,
                "maxResults": page_size,
            }
            if page_token:
                kwargs["pageToken"] = page_token
            resp = svc.users().messages().list(**kwargs).execute()
            batch = resp.get("messages") or []
            out.extend(batch)
            remaining -= len(batch)
            page_token = resp.get("nextPageToken")
            if not page_token or not batch:
                break
        return out[: max_results]
    except Exception as e:
        print(f"[gmail] list_messages error: {e}", file=sys.stderr)
        return []


def _walk_body(msg: Message) -> tuple[str, str]:
    body_text = ""
    body_html = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp:
                continue
            try:
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                text = payload.decode(charset, errors="replace")
            except Exception:
                continue
            if ctype == "text/plain" and not body_text:
                body_text = text
            elif ctype == "text/html" and not body_html:
                body_html = text
    else:
        try:
            payload = msg.get_payload(decode=True) or b""
            charset = msg.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
        except Exception:
            text = ""
        if msg.get_content_type() == "text/html":
            body_html = text
        else:
            body_text = text
    if not body_text and body_html:
        try:
            body_text = BeautifulSoup(body_html, "html.parser").get_text("\n")
        except Exception:
            body_text = body_html
    return body_text, body_html


def get_message(msg_id: str) -> dict[str, Any]:
    """Fetch a message by id and parse subject/from/date/body."""
    try:
        svc = gmail_service()
        raw = (
            svc.users()
            .messages()
            .get(userId="me", id=msg_id, format="raw")
            .execute()
        )
        raw_bytes = base64.urlsafe_b64decode(raw.get("raw", ""))
        msg = email.message_from_bytes(raw_bytes)
        body_text, body_html = _walk_body(msg)
        return {
            "message_id": msg_id,
            "thread_id": raw.get("threadId"),
            "subject": msg.get("Subject", ""),
            "from": msg.get("From", ""),
            "to": msg.get("To", ""),
            "cc": msg.get("Cc", ""),
            "date": msg.get("Date", ""),
            "body_text": body_text,
            "body_html": body_html,
        }
    except Exception as e:
        print(f"[gmail] get_message error: {e}", file=sys.stderr)
        return {"message_id": msg_id, "error": str(e)}


EXTRACT_SYSTEM = (
    "You extract structured CRM fields from email messages. "
    "Return valid JSON only matching the requested schema."
)


def extract_structured(message: dict[str, Any]) -> dict[str, Any]:
    """Use Gemini JSON mode to extract structured fields from an email."""
    prompt = f"""Extract structured data from this email.

Return JSON with keys:
sender_name, sender_email, sender_company, sender_title,
phone_numbers (array of strings),
other_emails_mentioned (array of strings),
meeting_requests (array of objects with date, time, purpose),
action_items (array of strings),
summary (string)

Email:
From: {message.get('from', '')}
Subject: {message.get('subject', '')}
Date: {message.get('date', '')}
Body:
{(message.get('body_text') or '')[:8000]}
"""
    try:
        raw = extract_json(prompt, system=EXTRACT_SYSTEM, max_tokens=1200)
        return json.loads(raw)
    except Exception as e:
        print(f"[gmail] extract_structured error: {e}", file=sys.stderr)
        return {"error": str(e)}


def extract_batch(
    query: str,
    max_results: int = 10,
    *,
    ai_extract: bool = True,
) -> list[dict[str, Any]]:
    """List messages; optionally run Gemini structured extraction on each."""
    out: list[dict[str, Any]] = []
    for stub in list_messages(query, max_results=max_results):
        mid = stub.get("id")
        if not mid:
            continue
        message = get_message(mid)
        if message.get("error"):
            out.append(
                {
                    "message_id": mid,
                    "subject": "",
                    "from": "",
                    "date": "",
                    "mailbox": "",
                    "extracted": {"error": message["error"]},
                }
            )
            continue
        if ai_extract:
            extracted = extract_structured(message)
        else:
            extracted = {
                "summary": (message.get("body_text") or "")[:280],
                "sender_name": "",
                "sender_company": "",
                "phone_numbers": [],
                "action_items": [],
            }
        out.append(
            {
                "message_id": mid,
                "thread_id": message.get("thread_id", ""),
                "subject": message.get("subject", ""),
                "from": message.get("from", ""),
                "to": message.get("to", ""),
                "cc": message.get("cc", ""),
                "date": message.get("date", ""),
                "body_text": (message.get("body_text") or "")[:4000],
                "mailbox": "",
                "extracted": extracted,
            }
        )
    return out


def filter_messages(
    messages: list[dict[str, Any]],
    needle: str,
) -> list[dict[str, Any]]:
    """Simple keyword filter over subject/from/to/body/summary."""
    q = (needle or "").strip().lower()
    if not q:
        return list(messages or [])
    terms = [t for t in re.split(r"\s+", q) if t]
    out: list[dict[str, Any]] = []
    for m in messages or []:
        ex = m.get("extracted") or {}
        blob = " ".join(
            [
                str(m.get("subject") or ""),
                str(m.get("from") or ""),
                str(m.get("to") or ""),
                str(m.get("mailbox") or ""),
                str(ex.get("summary") or ""),
                str(ex.get("sender_company") or ""),
                str(m.get("body_text") or "")[:1500],
            ]
        ).lower()
        if all(t in blob for t in terms):
            out.append(m)
    return out


def contacts_from_mailbox(
    messages: list[dict[str, Any]] | None,
    *,
    prefer: str = "auto",
) -> list[dict[str, Any]]:
    """Build personalized recipient rows from inbox/sent extract results.

    prefer: 'sent' → use To addresses; 'inbox' → use From; 'auto' → by mailbox tag.
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in messages or []:
        mailbox = (m.get("mailbox") or "").lower()
        use_to = prefer == "sent" or (prefer == "auto" and mailbox == "sent")
        header = (m.get("to") if use_to else m.get("from")) or ""
        if not header and use_to:
            header = m.get("from") or ""
        emails = re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", header)
        if not emails:
            continue
        email_addr = emails[0]
        key = email_addr.lower()
        if key in seen:
            continue
        seen.add(key)
        ex = m.get("extracted") or {}
        display = (ex.get("sender_name") or "").strip()
        if not display:
            before = header.split("<")[0].strip().strip('"')
            # Bare "a@b.com" headers must not become the Name field
            if before and "@" not in before:
                display = before
            else:
                local = email_addr.split("@")[0]
                display = " ".join(
                    p[:1].upper() + p[1:]
                    for p in re.split(r"[._+\-]+", local)
                    if p
                )
        if "@" in display:
            # Still looks like an address — humanize local-part only
            local = (emails[0] if "@" in display else email_addr).split("@")[0]
            display = " ".join(
                p[:1].upper() + p[1:]
                for p in re.split(r"[._+\-]+", local)
                if p
            )
        name_parts = display.split(None, 1)
        rows.append(
            {
                "email": email_addr,
                "name": display,
                "first_name": name_parts[0] if name_parts else "",
                "title": ex.get("sender_title") or "",
                "company": ex.get("sender_company") or "",
                "prior_subject": m.get("subject") or "",
                "prior_summary": (ex.get("summary") or m.get("body_text") or "")[:500],
                "mailbox": mailbox or ("sent" if use_to else "inbox"),
                "message_id": m.get("message_id") or "",
                "thread_id": m.get("thread_id") or "",
            }
        )
    return rows


def extract_inbox_and_sent(
    *,
    days: int = 30,
    max_per_mailbox: int = 100,
    ai_extract: bool = True,
    include_inbox: bool = True,
    include_sent: bool = True,
) -> list[dict[str, Any]]:
    """Pull inbox and/or sent messages, tag mailbox, dedupe by message_id."""
    queries: list[tuple[str, str]] = []
    if include_inbox:
        queries.append(("inbox", f"in:inbox newer_than:{max(1, days)}d"))
    if include_sent:
        queries.append(("sent", f"in:sent newer_than:{max(1, days)}d"))

    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for mailbox, q in queries:
        rows = extract_batch(q, max_results=max_per_mailbox, ai_extract=ai_extract)
        for row in rows:
            mid = row.get("message_id") or ""
            if mid and mid in seen:
                continue
            if mid:
                seen.add(mid)
            row["mailbox"] = mailbox
            out.append(row)
    return out
