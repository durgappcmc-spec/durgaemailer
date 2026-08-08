# NOTE: Register POST failures are swallowed so tracking outages never block sends.
from __future__ import annotations

import sys
import uuid
from typing import Any, Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from config import settings


def instrument_html(
    html: str,
    recipient_email: str,
    subject: Optional[str] = None,
    campaign: Optional[str] = None,
    prospect_source: Optional[str] = None,
    recipient_name: Optional[str] = None,
    track_clicks: bool = True,
    track_opens: bool = True,
) -> tuple[str, str]:
    """Rewrite links + append open pixel. Returns (html, email_id)."""
    email_id = str(uuid.uuid4())
    base = (settings.TRACKING_BASE_URL or "").rstrip("/")
    soup = BeautifulSoup(html or "", "html.parser")
    links: list[dict[str, str]] = []

    if track_clicks and base:
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href:
                continue
            lower = href.lower()
            if lower.startswith(("mailto:", "tel:", "#")):
                continue
            if base and base in href:
                continue
            link_id = str(uuid.uuid4())
            label = a.get_text(" ", strip=True)[:120]
            links.append(
                {"link_id": link_id, "original_url": href, "label": label}
            )
            a["href"] = f"{base}/t/c/{link_id}"

    instrumented = str(soup)
    if track_opens and base:
        pixel = (
            f'<img src="{base}/t/o/{email_id}.gif" width="1" height="1" '
            f'alt="" style="display:block;border:0;">'
        )
        if "</body>" in instrumented.lower():
            # Case-insensitive replace of closing body
            idx = instrumented.lower().rfind("</body>")
            instrumented = instrumented[:idx] + pixel + instrumented[idx:]
        else:
            instrumented = instrumented + pixel

    if settings.APPS_SCRIPT_TRACKING_URL:
        payload = {
            "action": "register",
            "email_id": email_id,
            "recipient_email": recipient_email,
            "recipient_name": recipient_name or "",
            "subject": subject or "",
            "campaign": campaign or "",
            "prospect_source": prospect_source or "",
            "links": links,
        }
        try:
            requests.post(
                settings.APPS_SCRIPT_TRACKING_URL,
                json=payload,
                timeout=15,
            )
        except Exception as e:
            print(f"[tracking] register failed: {e}", file=sys.stderr)

    return instrumented, email_id
