# NOTE: Raw MIME parse; HTML-only messages get text derived via BeautifulSoup.
from __future__ import annotations

import base64
import email
import json
import re
import sys
from email.header import decode_header, make_header
from email.message import Message
from typing import Any, Optional

from bs4 import BeautifulSoup

from core.llm import extract_json
from gmail_client.auth import gmail_service

# Keep enough of the body for like-sent cloning / research (was 4000).
_DEFAULT_BODY_LIMIT = 20000
_LIKE_SENT_BODY_LIMIT = 50000


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


def _decode_mime_header(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def _walk_body(msg: Message) -> tuple[str, str]:
    """Return (plain_text, html). Concatenate parts so long emails aren't truncated."""
    text_parts: list[str] = []
    html_parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                continue
            if ctype not in ("text/plain", "text/html"):
                continue
            try:
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                text = payload.decode(charset, errors="replace")
            except Exception:
                continue
            if ctype == "text/plain" and text.strip():
                text_parts.append(text)
            elif ctype == "text/html" and text.strip():
                html_parts.append(text)
    else:
        try:
            payload = msg.get_payload(decode=True) or b""
            charset = msg.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
        except Exception:
            text = ""
        if msg.get_content_type() == "text/html":
            if text.strip():
                html_parts.append(text)
        elif text.strip():
            text_parts.append(text)

    body_html = "\n".join(html_parts).strip()
    body_text = "\n\n".join(text_parts).strip()
    if not body_text and body_html:
        try:
            body_text = BeautifulSoup(body_html, "html.parser").get_text("\n")
        except Exception:
            body_text = body_html
    return body_text.strip(), body_html


def _decode_gmail_data(data: str) -> str:
    try:
        raw = base64.urlsafe_b64decode(data + "==")
        return raw.decode("utf-8", errors="replace")
    except Exception:
        try:
            return base64.urlsafe_b64decode(data + "==").decode(
                "latin-1", errors="replace"
            )
        except Exception:
            return ""


def _walk_gmail_payload(payload: dict[str, Any] | None) -> tuple[str, str]:
    """Parse Gmail API format=full payload tree into text + html."""
    if not payload:
        return "", ""
    text_parts: list[str] = []
    html_parts: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        if not isinstance(part, dict):
            return
        mime = (part.get("mimeType") or "").lower()
        filename = (part.get("filename") or "").strip()
        body = part.get("body") or {}
        data = body.get("data")
        if filename and mime.startswith("application/"):
            return
        if data and mime == "text/plain":
            text_parts.append(_decode_gmail_data(data))
        elif data and mime == "text/html":
            html_parts.append(_decode_gmail_data(data))
        for child in part.get("parts") or []:
            walk(child)

    walk(payload)
    body_html = "\n".join(p for p in html_parts if p.strip()).strip()
    body_text = "\n\n".join(p for p in text_parts if p.strip()).strip()
    if not body_text and body_html:
        try:
            body_text = BeautifulSoup(body_html, "html.parser").get_text("\n")
        except Exception:
            body_text = body_html
    return body_text.strip(), body_html


def get_message(msg_id: str) -> dict[str, Any]:
    """Fetch a message by id and parse subject/from/date/body (full content)."""
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

        # Fallback: format=full when raw MIME yielded an empty body
        if not (body_text or body_html):
            full = (
                svc.users()
                .messages()
                .get(userId="me", id=msg_id, format="full")
                .execute()
            )
            body_text, body_html = _walk_gmail_payload(full.get("payload"))
            headers = {
                (h.get("name") or "").lower(): h.get("value") or ""
                for h in (full.get("payload") or {}).get("headers") or []
            }
            return {
                "message_id": msg_id,
                "thread_id": full.get("threadId") or raw.get("threadId"),
                "subject": _decode_mime_header(headers.get("subject", "")),
                "from": _decode_mime_header(headers.get("from", "")),
                "to": _decode_mime_header(headers.get("to", "")),
                "cc": _decode_mime_header(headers.get("cc", "")),
                "date": headers.get("date", ""),
                "body_text": body_text,
                "body_html": body_html,
            }

        return {
            "message_id": msg_id,
            "thread_id": raw.get("threadId"),
            "subject": _decode_mime_header(msg.get("Subject", "")),
            "from": _decode_mime_header(msg.get("From", "")),
            "to": _decode_mime_header(msg.get("To", "")),
            "cc": _decode_mime_header(msg.get("Cc", "")),
            "date": msg.get("Date", "") or "",
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
    body_limit: int = _DEFAULT_BODY_LIMIT,
) -> list[dict[str, Any]]:
    """List messages; optionally run Gemini structured extraction on each."""
    out: list[dict[str, Any]] = []
    limit = max(500, int(body_limit or _DEFAULT_BODY_LIMIT))
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
                    "to": "",
                    "date": "",
                    "mailbox": "",
                    "body_text": "",
                    "body_html": "",
                    "extracted": {"error": message["error"]},
                }
            )
            continue
        body_text = message.get("body_text") or ""
        body_html = message.get("body_html") or ""
        if ai_extract:
            extracted = extract_structured(message)
        else:
            extracted = {
                "summary": body_text[:500],
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
                "body_text": body_text[:limit],
                "body_html": body_html[:limit],
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
                str(m.get("body_text") or "")[:8000],
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
        display = ""
        m_name = re.match(r'\s*"?([^"<]+?)"?\s*<', header)
        if m_name:
            display = m_name.group(1).strip()
        if not display:
            local = email_addr.split("@", 1)[0]
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


def _company_search_queries(company: str, days: int) -> list[str]:
    """Build several Gmail queries to find sent mail about a company or address."""
    company = (company or "").strip()
    if not company:
        return []
    days = max(1, int(days))
    base = f"in:sent newer_than:{days}d"

    # Recipient email → precise to: search
    if "@" in company and re.match(
        r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$", company
    ):
        return [
            f"{base} to:{company}",
            f'{base} "{company}"',
            f"{base} {company}",
        ]

    quoted = f'"{company}"'
    compact = re.sub(r"[^a-zA-Z0-9]", "", company).lower()
    queries = [
        f"{base} {quoted}",
        f"{base} to:{company.split()[0]}",
        f"{base} {company}",
    ]
    if compact and compact != company.lower():
        queries.append(f"{base} {compact}")
    if compact and len(compact) >= 3:
        queries.append(f"{base} to:{compact}.com")
        queries.append(f"{base} {compact}.com")
    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        key = q.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(q)
    return out


def find_sent_to_company(
    company: str,
    *,
    days: int = 365,
    max_results: int = 15,
    ai_extract: bool = False,
) -> list[dict[str, Any]]:
    """Find sent messages related to a company or recipient email — full body."""
    company = (company or "").strip()
    if not company:
        return []
    is_email = "@" in company
    seen_ids: set[str] = set()
    rows: list[dict[str, Any]] = []
    for query in _company_search_queries(company, days):
        batch = extract_batch(
            query,
            max_results=max_results,
            ai_extract=ai_extract,
            body_limit=_LIKE_SENT_BODY_LIMIT,
        )
        for row in batch:
            mid = row.get("message_id") or ""
            if mid and mid in seen_ids:
                continue
            if mid:
                seen_ids.add(mid)
            row["mailbox"] = "sent"
            rows.append(row)
        with_body = [
            r
            for r in rows
            if (r.get("body_text") or r.get("body_html") or "").strip()
        ]
        # One solid hit is enough when searching by exact recipient email
        if is_email and with_body:
            break
        if len(with_body) >= 3:
            break

    if is_email:
        email_l = company.lower()
        matched = [
            r
            for r in rows
            if email_l in str(r.get("to") or "").lower()
            or email_l in str(r.get("cc") or "").lower()
            or email_l in str(r.get("body_text") or "").lower()[:2000]
        ]
        candidates = matched or rows
    else:
        filtered = filter_messages(rows, company)
        candidates = filtered or rows

    refreshed: list[dict[str, Any]] = []
    for row in candidates[:max_results]:
        mid = row.get("message_id") or ""
        if not mid:
            refreshed.append(row)
            continue
        # Always re-fetch so like-sent gets full MIME (text + html), not a stub
        fresh = get_message(mid)
        if not fresh.get("error"):
            row = {
                **row,
                "subject": fresh.get("subject") or row.get("subject") or "",
                "from": fresh.get("from") or row.get("from") or "",
                "to": fresh.get("to") or row.get("to") or "",
                "cc": fresh.get("cc") or row.get("cc") or "",
                "date": fresh.get("date") or row.get("date") or "",
                "body_text": (fresh.get("body_text") or "")[:_LIKE_SENT_BODY_LIMIT],
                "body_html": (fresh.get("body_html") or "")[:_LIKE_SENT_BODY_LIMIT],
                "mailbox": "sent",
            }
            if not (row.get("extracted") or {}).get("summary"):
                row["extracted"] = {
                    **(row.get("extracted") or {}),
                    "summary": (row.get("body_text") or "")[:500],
                }
        refreshed.append(row)
    return refreshed


def pick_best_sent_reference(
    messages: list[dict[str, Any]] | None,
    company: str,
) -> Optional[dict[str, Any]]:
    """Score sent rows; prefer To/subject/body matches with a usable full body."""
    company_l = (company or "").strip().lower()
    is_email = "@" in company_l
    compact = re.sub(r"[^a-z0-9]", "", company_l)
    best: Optional[dict[str, Any]] = None
    best_score = -1
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        body = (m.get("body_text") or "").strip()
        html = (m.get("body_html") or "").strip()
        to_l = str(m.get("to") or "").lower()
        cc_l = str(m.get("cc") or "").lower()
        blob = " ".join(
            [
                to_l,
                cc_l,
                str(m.get("subject") or ""),
                body[:8000],
                html[:2000],
                str((m.get("extracted") or {}).get("summary") or ""),
                str((m.get("extracted") or {}).get("sender_company") or ""),
            ]
        ).lower()
        score = 0
        if is_email and company_l in to_l:
            score += 25
        elif is_email and company_l in cc_l:
            score += 15
        elif is_email and company_l in blob:
            score += 8
        if company_l and company_l in blob:
            score += 10
        if compact and compact in re.sub(r"[^a-z0-9]", "", blob):
            score += 6
        if not is_email:
            if company_l and company_l in to_l:
                score += 8
            if compact and compact in re.sub(r"[^a-z0-9@.]", "", to_l):
                score += 8
        if len(body) > 400:
            score += 8
        elif len(body) > 80:
            score += 4
        elif body or html:
            score += 1
        else:
            score -= 5
        if html and len(html) > 200:
            score += 2
        if (m.get("subject") or "").strip():
            score += 1
        if score > best_score:
            best_score = score
            best = m
    return best


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
