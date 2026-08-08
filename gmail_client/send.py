# NOTE: Tracking instrumentation runs before MIME build; failures do not block send.
from __future__ import annotations

import base64
import sys
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Optional

from gmail_client.auth import gmail_service
from tracking.instrument import instrument_html


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
    try:
        instrumented, tracking_id = instrument_html(
            html_body,
            recipient_email=to,
            subject=subject,
            campaign=campaign,
            prospect_source=source,
            recipient_name=recipient_name,
        )
    except Exception as e:
        print(f"[gmail] instrument failed, sending untracked: {e}", file=sys.stderr)
        instrumented, tracking_id = html_body, ""

    msg = MIMEMultipart("alternative")
    msg["To"] = to
    msg["Subject"] = subject
    if recipient_name:
        msg["X-Recipient-Name"] = recipient_name
    msg.attach(MIMEText(instrumented, "html", "utf-8"))

    if attachments:
        # Upgrade to mixed if we have binary attachments
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
