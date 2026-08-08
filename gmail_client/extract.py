# NOTE: Raw MIME parse; HTML-only messages get text derived via BeautifulSoup.
from __future__ import annotations

import base64
import email
import json
import sys
from email.message import Message
from typing import Any, Optional

from bs4 import BeautifulSoup

from core.llm import extract_json
from gmail_client.auth import gmail_service


def list_messages(query: str, max_results: int = 20) -> list[dict[str, str]]:
    """List Gmail message ids matching a search query."""
    try:
        svc = gmail_service()
        resp = (
            svc.users()
            .messages()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        )
        return resp.get("messages") or []
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


def extract_batch(query: str, max_results: int = 10) -> list[dict[str, Any]]:
    """List messages, extract structured data for each."""
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
                    "extracted": {"error": message["error"]},
                }
            )
            continue
        extracted = extract_structured(message)
        out.append(
            {
                "message_id": mid,
                "subject": message.get("subject", ""),
                "from": message.get("from", ""),
                "date": message.get("date", ""),
                "extracted": extracted,
            }
        )
    return out
