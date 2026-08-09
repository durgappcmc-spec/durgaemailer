# NOTE: Tracking instrumentation runs before MIME build; failures do not block send/draft.
# Drafts default to GMAIL_FROM_EMAIL (csr@…) with the Gmail send-as signature appended.
from __future__ import annotations

import base64
import re
import sys
import time
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Optional

from config import settings
from gmail_client.auth import gmail_service
from tracking.instrument import instrument_html

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_signature_cache: dict[str, tuple[float, str]] = {}


def default_from_email() -> str:
    return (
        (getattr(settings, "GMAIL_FROM_EMAIL", "") or "").strip()
        or "csr@karunamedia.org"
    )


def default_cc_emails() -> list[str]:
    raw = (getattr(settings, "GMAIL_DEFAULT_CC", "") or "").strip()
    if not raw:
        return []
    return [e.lower() for e in _EMAIL_RE.findall(raw)]


def get_signature(send_as_email: Optional[str] = None) -> str:
    """Return HTML signature for a send-as address (cached ~30 min)."""
    email = (send_as_email or default_from_email()).strip().lower()
    now = time.time()
    cached = _signature_cache.get(email)
    if cached and now - cached[0] < 30 * 60:
        return cached[1]
    try:
        svc = gmail_service()
        # Prefer exact send-as record
        try:
            row = (
                svc.users()
                .settings()
                .sendAs()
                .get(userId="me", sendAsEmail=email)
                .execute()
            )
            sig = row.get("signature") or ""
        except Exception:
            sig = ""
            rows = (
                svc.users().settings().sendAs().list(userId="me").execute().get("sendAs")
                or []
            )
            for r in rows:
                if (r.get("sendAsEmail") or "").lower() == email:
                    sig = r.get("signature") or ""
                    break
            if not sig:
                for r in rows:
                    if r.get("isPrimary") or r.get("isDefault"):
                        sig = r.get("signature") or ""
                        if sig:
                            break
        _signature_cache[email] = (now, sig)
        return sig
    except Exception as e:
        print(f"[gmail] signature fetch error: {e}", file=sys.stderr)
        return ""


def append_signature(html_body: str, *, from_email: Optional[str] = None) -> str:
    """Append the Gmail send-as signature if not already present."""
    body = html_body or "<p></p>"
    sig = get_signature(from_email)
    if not sig:
        return body
    # Avoid duplicating if body already contains a chunk of the signature
    needle = re.sub(r"\s+", "", sig[:80]).lower()
    compact_body = re.sub(r"\s+", "", body).lower()
    if needle and needle in compact_body:
        return body
    return f"{body}<br><br>{sig}"


def _normalize_cc(cc: Optional[str | list[str]]) -> str:
    if not cc:
        return ""
    if isinstance(cc, str):
        emails = _EMAIL_RE.findall(cc)
    else:
        emails = []
        for item in cc:
            emails.extend(_EMAIL_RE.findall(str(item)))
    # Dedupe, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for e in emails:
        key = e.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return ", ".join(out)


def _build_raw_message(
    to: str,
    subject: str,
    html_body: str,
    recipient_name: Optional[str] = None,
    attachments: Optional[list[dict[str, Any]]] = None,
    *,
    instrument: bool = True,
    campaign: Optional[str] = None,
    source: Optional[str] = None,
    from_email: Optional[str] = None,
    cc: Optional[str | list[str]] = None,
    include_signature: bool = True,
) -> tuple[str, str]:
    """Build base64url raw MIME. Returns (raw, tracking_id)."""
    tracking_id = ""
    from_addr = (from_email or default_from_email()).strip()
    body = html_body
    if include_signature:
        body = append_signature(body, from_email=from_addr)
    if instrument:
        try:
            body, tracking_id = instrument_html(
                body,
                recipient_email=to,
                subject=subject,
                campaign=campaign,
                prospect_source=source,
                recipient_name=recipient_name,
            )
        except Exception as e:
            print(f"[gmail] instrument failed, continuing untracked: {e}", file=sys.stderr)
            tracking_id = ""

    cc_header = _normalize_cc(cc)
    msg = MIMEMultipart("alternative")
    if from_addr:
        msg["From"] = from_addr
    msg["To"] = to
    if cc_header:
        msg["Cc"] = cc_header
    msg["Subject"] = subject
    if recipient_name:
        msg["X-Recipient-Name"] = recipient_name
    msg.attach(MIMEText(body, "html", "utf-8"))

    if attachments:
        mixed = MIMEMultipart("mixed")
        if from_addr:
            mixed["From"] = from_addr
        mixed["To"] = to
        if cc_header:
            mixed["Cc"] = cc_header
        mixed["Subject"] = subject
        mixed.attach(msg)
        for att in attachments:
            part = MIMEApplication(att.get("data") or b"", Name=att.get("name") or "file")
            part["Content-Disposition"] = (
                f'attachment; filename="{att.get("name") or "file"}"'
            )
            mixed.attach(part)
        msg = mixed

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    return raw, tracking_id


def send_email(
    to: str,
    subject: str,
    html_body: str,
    recipient_name: Optional[str] = None,
    attachments: Optional[list[dict[str, Any]]] = None,
    campaign: Optional[str] = None,
    source: Optional[str] = None,
    *,
    from_email: Optional[str] = None,
    cc: Optional[str | list[str]] = None,
    include_signature: bool = True,
) -> dict[str, Any]:
    """Send an HTML email via Gmail with open/click tracking."""
    raw, tracking_id = _build_raw_message(
        to,
        subject,
        html_body,
        recipient_name=recipient_name,
        attachments=attachments,
        instrument=True,
        campaign=campaign,
        source=source,
        from_email=from_email,
        cc=cc,
        include_signature=include_signature,
    )
    try:
        svc = gmail_service()
        sent = (
            svc.users()
            .messages()
            .send(userId="me", body={"raw": raw})
            .execute()
        )
        return {
            "message_id": sent.get("id"),
            "thread_id": sent.get("threadId"),
            "tracking_id": tracking_id,
            "from": from_email or default_from_email(),
            "to": to,
            "cc": _normalize_cc(cc),
        }
    except Exception as e:
        print(f"[gmail] send error: {e}", file=sys.stderr)
        return {"error": str(e), "tracking_id": tracking_id}


def create_draft(
    to: str,
    subject: str,
    html_body: str,
    recipient_name: Optional[str] = None,
    attachments: Optional[list[dict[str, Any]]] = None,
    *,
    track: bool = False,
    campaign: Optional[str] = None,
    source: Optional[str] = None,
    from_email: Optional[str] = None,
    cc: Optional[str | list[str]] = None,
    include_signature: bool = True,
) -> dict[str, Any]:
    """Create a new Gmail draft for review (does not send)."""
    from_addr = from_email or default_from_email()
    raw, tracking_id = _build_raw_message(
        to,
        subject,
        html_body,
        recipient_name=recipient_name,
        attachments=attachments,
        instrument=track,
        campaign=campaign,
        source=source,
        from_email=from_addr,
        cc=cc,
        include_signature=include_signature,
    )
    try:
        svc = gmail_service()
        draft = (
            svc.users()
            .drafts()
            .create(userId="me", body={"message": {"raw": raw}})
            .execute()
        )
        message = draft.get("message") or {}
        return {
            "draft_id": draft.get("id"),
            "message_id": message.get("id"),
            "thread_id": message.get("threadId"),
            "tracking_id": tracking_id,
            "from": from_addr,
            "to": to,
            "cc": _normalize_cc(cc),
            "subject": subject,
        }
    except Exception as e:
        print(f"[gmail] create_draft error: {e}", file=sys.stderr)
        return {
            "error": str(e),
            "tracking_id": tracking_id,
            "from": from_addr,
            "to": to,
            "cc": _normalize_cc(cc),
            "subject": subject,
        }


def create_drafts(
    jobs: list[dict[str, Any]],
    *,
    track: bool = False,
) -> list[dict[str, Any]]:
    """Create many Gmail drafts. Each job: to/recipient_email, subject, html_body, …"""
    results: list[dict[str, Any]] = []
    for job in jobs:
        to = (job.get("to") or job.get("recipient_email") or "").strip()
        if not to:
            results.append({"error": "missing recipient_email", "job": job})
            continue
        results.append(
            create_draft(
                to=to,
                subject=job.get("subject") or "(no subject)",
                html_body=job.get("html_body") or job.get("body") or "<p></p>",
                recipient_name=job.get("recipient_name") or job.get("name"),
                attachments=job.get("attachments"),
                track=track,
                campaign=job.get("campaign"),
                source=job.get("source") or "chat_draft_batch",
                from_email=job.get("from_email") or job.get("from"),
                cc=job.get("cc") or job.get("cc_emails"),
                include_signature=job.get("include_signature", True),
            )
        )
    return results
