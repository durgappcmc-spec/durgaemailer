# NOTE: Tracking instrumentation runs before MIME build; failures do not block send/draft.
from __future__ import annotations

import base64
import sys
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Optional

from gmail_client.auth import gmail_service
from tracking.instrument import instrument_html


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
) -> tuple[str, str]:
    """Build base64url raw MIME. Returns (raw, tracking_id)."""
    tracking_id = ""
    body = html_body
    if instrument:
        try:
            body, tracking_id = instrument_html(
                html_body,
                recipient_email=to,
                subject=subject,
                campaign=campaign,
                prospect_source=source,
                recipient_name=recipient_name,
            )
        except Exception as e:
            print(f"[gmail] instrument failed, continuing untracked: {e}", file=sys.stderr)
            body, tracking_id = html_body, ""

    msg = MIMEMultipart("alternative")
    msg["To"] = to
    msg["Subject"] = subject
    if recipient_name:
        msg["X-Recipient-Name"] = recipient_name
    msg.attach(MIMEText(body, "html", "utf-8"))

    if attachments:
        mixed = MIMEMultipart("mixed")
        mixed["To"] = to
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
) -> dict[str, Any]:
    """Create a Gmail draft (does not send). Tracking off by default for drafts."""
    raw, tracking_id = _build_raw_message(
        to,
        subject,
        html_body,
        recipient_name=recipient_name,
        attachments=attachments,
        instrument=track,
        campaign=campaign,
        source=source,
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
            "to": to,
            "subject": subject,
        }
    except Exception as e:
        print(f"[gmail] create_draft error: {e}", file=sys.stderr)
        return {"error": str(e), "tracking_id": tracking_id}
