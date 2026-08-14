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
    """Append the Gmail send-as signature once — never duplicate."""
    from gmail_client.html_format import body_looks_signed, normalize_email_html

    body = normalize_email_html(html_body or "<p></p>")
    sig = get_signature(from_email)
    if not sig:
        return body
    if body_looks_signed(body, sig):
        return body
    # Legacy short-needle check
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
    track_clicks: bool = True,
    track_opens: bool = True,
    campaign: Optional[str] = None,
    source: Optional[str] = None,
    from_email: Optional[str] = None,
    cc: Optional[str | list[str]] = None,
    include_signature: bool = True,
    plain_body: Optional[str] = None,
    bcc: Optional[str | list[str]] = None,
) -> tuple[str, str]:
    """Build base64url raw MIME. Returns (raw, tracking_id).

    multipart/alternative: text/plain first (cleaned prose), then text/html.
    Never calls textwrap.fill().
    """
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
                track_clicks=track_clicks,
                track_opens=track_opens,
            )
        except Exception as e:
            print(f"[gmail] instrument failed, continuing untracked: {e}", file=sys.stderr)
            tracking_id = ""

    cc_header = _normalize_cc(cc)
    bcc_header = _normalize_cc(bcc)
    msg = MIMEMultipart("alternative")
    if from_addr:
        msg["From"] = from_addr
    msg["To"] = to
    if cc_header:
        msg["Cc"] = cc_header
    if bcc_header:
        msg["Bcc"] = bcc_header
    msg["Subject"] = subject
    if recipient_name:
        msg["X-Recipient-Name"] = recipient_name
    if plain_body is not None:
        msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(body, "html", "utf-8"))

    if attachments:
        mixed = MIMEMultipart("mixed")
        if from_addr:
            mixed["From"] = from_addr
        mixed["To"] = to
        if cc_header:
            mixed["Cc"] = cc_header
        if bcc_header:
            mixed["Bcc"] = bcc_header
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


def _verify_gmail_plain_matches(gmail_draft_id: str, body_cleaned: str) -> None:
    """Re-fetch the draft; warn (do not raise) if text/plain ≠ body_cleaned."""
    did = (gmail_draft_id or "").removeprefix("gmail:")
    if not did:
        return
    try:
        from gmail_client.drafts import _extract_bodies

        svc = gmail_service()
        full = (
            svc.users()
            .drafts()
            .get(userId="me", id=did, format="full")
            .execute()
        )
        _html, text = _extract_bodies((full.get("message") or {}).get("payload") or {})
        fetched = (text or "").strip()
        expected = (body_cleaned or "").strip()
        if fetched != expected:
            print(
                "[gmail] Draft preview does not match Gmail draft "
                f"(plain {len(fetched)} chars vs cleaned {len(expected)})",
                file=sys.stderr,
            )
    except Exception as e:
        print(f"[gmail] draft round-trip check skipped: {e}", file=sys.stderr)


def create_draft(
    to: str,
    subject: str,
    html_body: str,
    recipient_name: Optional[str] = None,
    attachments: Optional[list[dict[str, Any]]] = None,
    *,
    track: bool = True,
    campaign: Optional[str] = None,
    source: Optional[str] = None,
    from_email: Optional[str] = None,
    cc: Optional[str | list[str]] = None,
    include_signature: bool = True,
    recipient_title: Optional[str] = None,
    company: Optional[str] = None,
    bcc: Optional[str | list[str]] = None,
) -> dict[str, Any]:
    """Create a new Gmail draft for review.

    Open-pixel tracking is embedded (hidden). Click links stay as original
    URLs in the draft so Netlify tracking URLs are not visible while reviewing;
    clicks are wrapped at send time.
    """
    from gmail_client.html_format import prepare_draft_bodies

    from_addr = from_email or default_from_email()
    src = (source or "relay_draft").strip() or "relay_draft"
    if track and "draft" not in src.lower():
        src = f"{src}_draft"

    # Clean LLM/plain/HTML → body_cleaned (source of truth) + simple HTML
    body_cleaned, html_core = prepare_draft_bodies(html_body or "")
    body = html_core
    try:
        from core.tracking import strip_tracking, strip_visible_tracking_urls

        body = strip_visible_tracking_urls(strip_tracking(body))
    except Exception:
        pass
    if include_signature:
        body = append_signature(body, from_email=from_addr)

    # Drafts: open pixel only — never rewrite hrefs to Netlify click URLs
    raw, tracking_id = _build_raw_message(
        to,
        subject,
        body,
        recipient_name=recipient_name,
        attachments=attachments,
        instrument=track,
        track_clicks=False,
        track_opens=True,
        campaign=campaign,
        source=src,
        from_email=from_addr,
        cc=cc,
        include_signature=False,  # already applied above — do not double-insert
        plain_body=body_cleaned,
        bcc=bcc,
    )
    # Drive mirror uses the same signed body (no second append_signature)
    instrumented_html = body
    if track:
        try:
            from core.tracking import inject_tracking

            instrumented_html, tracking_id = inject_tracking(
                body,
                tracking_id=tracking_id or None,
                recipient_email=to,
                subject=subject,
                register=False,
                track_clicks=False,
                track_opens=True,
            )
        except Exception:
            pass
    try:
        svc = gmail_service()
        draft = (
            svc.users()
            .drafts()
            .create(userId="me", body={"message": {"raw": raw}})
            .execute()
        )
        message = draft.get("message") or {}
        result = {
            "draft_id": draft.get("id"),
            "message_id": message.get("id"),
            "thread_id": message.get("threadId"),
            "tracking_id": tracking_id,
            "tracked": bool(track and tracking_id),
            "from": from_addr,
            "to": to,
            "cc": _normalize_cc(cc),
            "subject": subject,
            "body_html": instrumented_html,
            "body_cleaned": body_cleaned,
            "title": (recipient_title or "").strip(),
            "company": (company or "").strip(),
        }
        _verify_gmail_plain_matches(result.get("draft_id") or "", body_cleaned)
        _mirror_draft_to_drive(
            result,
            recipient_name=recipient_name,
            recipient_title=recipient_title,
            company=company,
            source=src,
        )
        return result
    except Exception as e:
        print(f"[gmail] create_draft error: {e}", file=sys.stderr)
        # Still land on Drafts page via Drive so composed emails are not lost
        fallback = _save_drive_only_draft(
            to=to,
            subject=subject,
            html_body=instrumented_html,
            recipient_name=recipient_name,
            recipient_title=recipient_title,
            company=company,
            from_email=from_addr,
            cc=cc,
            tracking_id=tracking_id,
            source=src,
            gmail_error=str(e),
        )
        if fallback.get("draft_id"):
            fallback["gmail_error"] = str(e)
            return fallback
        return {
            "error": str(e),
            "tracking_id": tracking_id,
            "tracked": False,
            "from": from_addr,
            "to": to,
            "cc": _normalize_cc(cc),
            "subject": subject,
            "body_cleaned": body_cleaned,
        }


def _mirror_draft_to_drive(
    result: dict[str, Any],
    *,
    recipient_name: Optional[str] = None,
    recipient_title: Optional[str] = None,
    company: Optional[str] = None,
    source: str = "",
) -> None:
    """Persist Chat/Schedule drafts so the Drafts page can list/send them."""
    gmail_id = result.get("draft_id")
    if not gmail_id or result.get("error"):
        return
    try:
        from core import drive_db

        drive_id = f"gmail:{gmail_id}"
        title = (
            (recipient_title or "").strip()
            or str(result.get("title") or result.get("designation") or "").strip()
        )
        co = (company or "").strip() or str(result.get("company") or "").strip()
        drive_db.save_draft(
            drive_id,
            {
                "draft_id": drive_id,
                "gmail_draft_id": gmail_id,
                "gmail_message_id": result.get("message_id") or "",
                "to": result.get("to") or "",
                "recipient": result.get("to") or "",
                "recipient_name": recipient_name or "",
                "title": title,
                "designation": title,
                "company": co,
                "cc": result.get("cc") or "",
                "subject": result.get("subject") or "",
                "body_html": result.get("body_html") or "",
                "body_cleaned": result.get("body_cleaned") or "",
                "tracking_id": result.get("tracking_id") or "",
                "status": "draft",
                "source": source or "gmail_create_draft",
                "from": result.get("from") or "",
            },
        )
    except Exception as e:
        print(f"[gmail] drive draft mirror skipped: {e}", file=sys.stderr)


def _save_drive_only_draft(
    *,
    to: str,
    subject: str,
    html_body: str,
    recipient_name: Optional[str] = None,
    recipient_title: Optional[str] = None,
    company: Optional[str] = None,
    from_email: Optional[str] = None,
    cc: Optional[str | list[str]] = None,
    tracking_id: Optional[str] = None,
    source: str = "",
    gmail_error: str = "",
) -> dict[str, Any]:
    """Persist a draft to Drive when Gmail create is unavailable."""
    import uuid

    try:
        from core import drive_db

        drive_id = f"draft_{uuid.uuid4().hex[:12]}"
        title = (recipient_title or "").strip()
        co = (company or "").strip()
        payload = {
            "draft_id": drive_id,
            "to": to or "",
            "recipient": to or "",
            "recipient_name": recipient_name or "",
            "title": title,
            "designation": title,
            "company": co,
            "cc": _normalize_cc(cc),
            "subject": subject or "(no subject)",
            "body_html": html_body or "",
            "body_cleaned": "",
            "tracking_id": tracking_id or "",
            "status": "draft",
            "source": source or "drive_fallback_draft",
            "from": from_email or "",
            "gmail_error": (gmail_error or "")[:500],
        }
        drive_db.save_draft(drive_id, payload)
        return {
            "draft_id": drive_id,
            "tracking_id": tracking_id or "",
            "tracked": bool(tracking_id),
            "from": from_email or "",
            "to": to or "",
            "cc": _normalize_cc(cc),
            "subject": subject or "(no subject)",
            "body_html": html_body or "",
            "body_cleaned": "",
            "title": title,
            "company": co,
            "drive_only": True,
        }
    except Exception as e:
        print(f"[gmail] drive-only draft save failed: {e}", file=sys.stderr)
        return {"error": str(e)}


# Public alias for chat/router fallbacks
save_drive_only_draft = _save_drive_only_draft


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
    tracking_id: Optional[str] = None,
) -> dict[str, Any]:
    """Send an HTML email via Gmail with open/click tracking.

    If tracking_id is provided, re-inject that same id (preserve draft tracking).
    """
    body = html_body
    tid = tracking_id or ""
    if tid:
        try:
            from core.tracking import inject_tracking

            body, tid = inject_tracking(
                body,
                tracking_id=tid,
                recipient_email=to,
                subject=subject or "",
                prospect_source=source or "relay_send",
                recipient_name=recipient_name or "",
                register=True,
            )
        except Exception as e:
            print(f"[gmail] preserve-tracking inject failed: {e}", file=sys.stderr)
        raw, built_tid = _build_raw_message(
            to,
            subject,
            body,
            recipient_name=recipient_name,
            attachments=attachments,
            instrument=False,
            campaign=campaign,
            source=source,
            from_email=from_email,
            cc=cc,
            include_signature=include_signature,
        )
        tracking_id = tid or built_tid
    else:
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


def create_drafts(
    jobs: list[dict[str, Any]],
    *,
    track: bool = True,
) -> list[dict[str, Any]]:
    """Create many Gmail drafts (tracked by default for post-send analytics)."""
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
                recipient_title=job.get("title")
                or job.get("designation")
                or job.get("recipient_title")
                or "",
                company=job.get("company") or "",
                attachments=job.get("attachments"),
                track=job.get("track", track),
                campaign=job.get("campaign"),
                source=job.get("source") or "chat_draft_batch",
                from_email=job.get("from_email") or job.get("from"),
                cc=job.get("cc") or job.get("cc_emails"),
                include_signature=job.get("include_signature", True),
            )
        )
    return results


def send_bulk_serial(
    jobs: list[dict[str, Any]],
    *,
    jitter_seconds: tuple[float, float] = (2.0, 5.0),
) -> list[dict[str, Any]]:
    """Send many emails serially with jitter; isolate per-draft failures.

    Attachments may be Drive refs: {"drive_name": "...", "filename": "..."} —
    bytes are streamed at send time (25 MB cap).
    """
    import random

    results: list[dict[str, Any]] = []
    for job in jobs:
        to = (job.get("to") or job.get("recipient_email") or "").strip()
        if not to:
            results.append({"error": "missing recipient", "job_id": job.get("draft_id")})
            continue
        try:
            attachments = _resolve_attachments(job.get("attachments") or [])
            total = sum(len(a.get("data") or b"") for a in attachments)
            if total > 25 * 1024 * 1024:
                results.append(
                    {
                        "error": "attachments exceed 25 MB",
                        "draft_id": job.get("draft_id"),
                        "to": to,
                    }
                )
                continue
            # Preserve tracking_id by injecting before send when provided
            html = job.get("html_body") or job.get("body_html") or job.get("body") or "<p></p>"
            tracking_id = job.get("tracking_id")
            if tracking_id:
                from core.tracking import inject_tracking

                html, tracking_id = inject_tracking(
                    html,
                    tracking_id=tracking_id,
                    recipient_email=to,
                    subject=job.get("subject") or "",
                    register=False,
                )
            result = send_email(
                to=to,
                subject=job.get("subject") or "(no subject)",
                html_body=html,
                recipient_name=job.get("recipient_name") or job.get("name"),
                attachments=attachments,
                campaign=job.get("campaign"),
                source=job.get("source") or "bulk_send",
                from_email=job.get("from_email") or job.get("from"),
                cc=job.get("cc") or job.get("cc_emails"),
                include_signature=job.get("include_signature", True),
                tracking_id=tracking_id,
            )
            if tracking_id and not result.get("tracking_id"):
                result["tracking_id"] = tracking_id
            result["draft_id"] = job.get("draft_id")
            results.append(result)
        except Exception as e:
            results.append(
                {
                    "error": str(e),
                    "draft_id": job.get("draft_id"),
                    "to": to,
                }
            )
        lo, hi = jitter_seconds
        time.sleep(random.uniform(lo, hi))
    return results


def _resolve_attachments(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Resolve Drive-backed or inline attachments to {filename, data, mime_type}."""
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("data"):
            out.append(item)
            continue
        drive_name = item.get("drive_name") or item.get("drive_path")
        if not drive_name:
            continue
        try:
            from core import drive_store

            payload = drive_store.download_json(drive_name)
            # if stored as {filename, b64, mime_type}
            if isinstance(payload, dict) and payload.get("b64"):
                import base64 as b64

                out.append(
                    {
                        "filename": item.get("filename") or payload.get("filename") or "file.bin",
                        "data": b64.b64decode(payload["b64"]),
                        "mime_type": item.get("mime_type") or payload.get("mime_type") or "application/octet-stream",
                    }
                )
        except Exception as e:
            print(f"[gmail] attachment resolve failed: {e}", file=sys.stderr)
    return out
