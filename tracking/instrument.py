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
    email_id: Optional[str] = None,
) -> tuple[str, str]:
    """Rewrite links + append open pixel. Returns (html, email_id)."""
    email_id = (email_id or "").strip() or str(uuid.uuid4())
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
            # Keep original for draft preview / strip_tracking restore
            a["data-original-url"] = href
            # Query-param form is reliable on Netlify (path splat can drop id)
            a["href"] = f"{base}/.netlify/functions/click?id={link_id}"

    instrumented = str(soup)
    if track_opens and base and "/.netlify/functions/open" not in instrumented and f"{base}/t/o/" not in instrumented:
        # Hidden 1×1 open pixel — must not surface as a visible Netlify link
        pixel = (
            f'<img src="{base}/.netlify/functions/open?id={email_id}" '
            f'width="1" height="1" alt="" '
            f'style="display:none;width:1px;height:1px;border:0;opacity:0;" '
            f'data-tracking="open">'
        )
        if "</body>" in instrumented.lower():
            # Case-insensitive replace of closing body
            idx = instrumented.lower().rfind("</body>")
            instrumented = instrumented[:idx] + pixel + instrumented[idx:]
        else:
            instrumented = instrumented + pixel

    src = prospect_source or "relay_draft"
    if settings.APPS_SCRIPT_TRACKING_URL:
        payload = {
            "action": "register",
            "email_id": email_id,
            "recipient_email": recipient_email,
            "recipient_name": recipient_name or "",
            "subject": subject or "",
            "campaign": campaign or "",
            "prospect_source": src,
            "source": src,
            "links": links,
        }
        try:
            resp = requests.post(
                settings.APPS_SCRIPT_TRACKING_URL,
                json=payload,
                timeout=15,
            )
            if not resp.ok:
                print(
                    f"[tracking] register HTTP {resp.status_code}: {resp.text[:200]}",
                    file=sys.stderr,
                )
            else:
                try:
                    data = resp.json()
                    if not data.get("ok", True):
                        print(f"[tracking] register rejected: {data}", file=sys.stderr)
                except Exception:
                    pass
        except Exception as e:
            print(f"[tracking] register failed: {e}", file=sys.stderr)

    # Apps Script web app may lag behind repo — also seed Sends via Sheets API when possible
    try:
        from tracking.sheets_seed import seed_send_row

        seed_send_row(
            email_id=email_id,
            recipient_email=recipient_email,
            recipient_name=recipient_name or "",
            subject=subject or "",
            campaign=campaign or "",
            source=src,
        )
    except Exception as e:
        print(f"[tracking] sheets seed skipped: {e}", file=sys.stderr)

    return instrumented, email_id