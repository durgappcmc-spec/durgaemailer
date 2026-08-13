# NOTE: Strip/inject tracking helpers; wraps tracking.instrument without changing URL format.
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from config import settings
from tracking.instrument import instrument_html

_OPEN_MARKERS = (
    "/.netlify/functions/open",
    "/t/o/",
)
_CLICK_MARKERS = (
    "/.netlify/functions/click",
    "/t/c/",
)


def _tracking_base() -> str:
    return (settings.TRACKING_BASE_URL or "").rstrip("/")


def extract_tracking_id(html: str) -> Optional[str]:
    """Return existing open-pixel email_id / tracking_id if present."""
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    for img in soup.find_all("img", src=True):
        src = img["src"]
        if any(m in src for m in _OPEN_MARKERS):
            parsed = urlparse(src)
            qs = parse_qs(parsed.query)
            if qs.get("id"):
                return qs["id"][0]
            # path form /t/o/{id}
            parts = [p for p in parsed.path.split("/") if p]
            if parts:
                return parts[-1]
    # regex fallback
    m = re.search(
        r"(?:/\.netlify/functions/open|/t/o/)(?:\?id=)?([0-9a-fA-F\-]{8,})",
        html,
    )
    if m:
        return m.group(1)
    return None


_VISIBLE_TRACKING_URL_RE = re.compile(
    r"(?:&lt;|<)?https?://[^\s<>\"']*?"
    r"(?:durgaemailer-tracking\.netlify\.app|"
    r"/\.netlify/functions/(?:click|open)|/t/[co]/)"
    r"[^\s<>\"']*(?:&gt;|>)?",
    re.I,
)


def is_tracking_url(url: str) -> bool:
    href = (url or "").strip()
    if not href:
        return False
    if any(m in href for m in _OPEN_MARKERS + _CLICK_MARKERS):
        return True
    if "durgaemailer-tracking.netlify.app" in href.lower():
        return True
    base = _tracking_base()
    return bool(base) and base in href and ("click" in href or "open" in href)


def strip_visible_tracking_urls(text: str) -> str:
    """Remove tracking URLs that leaked into visible body text, including <url> autolinks.

    Gmail's text/plain part of a sent email often looks like:
    `our program <https://durgaemailer-tracking.netlify.app/.netlify/functions/click?id=…>`
    Cloning that into a new draft must not show the Netlify URL to the reviewer.
    """
    if not text:
        return text or ""
    out = _VISIBLE_TRACKING_URL_RE.sub("", text)
    base = _tracking_base()
    if base:
        out = re.sub(
            rf"(?:&lt;|<)?{re.escape(base)}[^\s<>\"']*(?:&gt;|>)?",
            "",
            out,
            flags=re.I,
        )
    out = re.sub(r"[ \t]*<[ \t]*>", "", out)
    out = re.sub(r"\(\s*\)", "", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out


def strip_tracking(html: str) -> str:
    """Remove open pixels and unwrap click-tracked hrefs back to originals.

    Used for draft preview/edit so Netlify tracking URLs stay hidden from the user.
    """
    if not html:
        return html or ""
    soup = BeautifulSoup(html, "html.parser")
    base = _tracking_base()

    for img in list(soup.find_all("img", src=True)):
        src = img.get("src") or ""
        if any(m in src for m in _OPEN_MARKERS) or img.get("data-tracking") == "open":
            img.decompose()

    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        is_click = is_tracking_url(href) and (
            any(m in href for m in _CLICK_MARKERS)
            or (bool(base) and base in href and "click" in href)
            or "durgaemailer-tracking.netlify.app" in href.lower()
        )
        if not is_click:
            continue
        original = a.get("data-original-url")
        if original and not is_tracking_url(original):
            a["href"] = original
            if a.has_attr("data-original-url"):
                del a["data-original-url"]
            continue
        # No original stored — drop the broken Netlify href rather than show it
        # (keep link text so the email still reads naturally)
        label = a.get_text(" ", strip=True)
        if is_tracking_url(label):
            a.decompose()
            continue
        a["href"] = "#"
        if a.has_attr("data-original-url"):
            del a["data-original-url"]

    from bs4 import NavigableString

    for node in list(soup.find_all(string=True)):
        if not isinstance(node, NavigableString):
            continue
        parent = node.parent
        if parent is None or parent.name in ("script", "style", "code", "pre"):
            continue
        raw = str(node)
        cleaned = strip_visible_tracking_urls(raw)
        if cleaned != raw:
            node.replace_with(cleaned)

    return strip_visible_tracking_urls(str(soup))


def inject_tracking(
    body_html: str,
    tracking_id: Optional[str] = None,
    *,
    recipient_email: str = "",
    subject: str = "",
    campaign: str = "",
    prospect_source: str = "relay_draft",
    recipient_name: str = "",
    track_clicks: bool = True,
    track_opens: bool = True,
    register: bool = True,
) -> tuple[str, str]:
    """Idempotent strip → inject. Preserves tracking_id when provided.

    For drafts, pass track_clicks=False so Netlify click URLs are not shown
    in Gmail/Drafts preview (open pixel stays hidden as a 1×1 image).
    """
    cleaned = strip_tracking(body_html or "")
    existing = tracking_id or extract_tracking_id(body_html or "")

    if not register:
        return _inject_local(
            cleaned,
            existing,
            track_clicks=track_clicks,
            track_opens=track_opens,
        )

    if existing:
        return _inject_local(
            cleaned,
            existing,
            track_clicks=track_clicks,
            track_opens=track_opens,
        )

    return instrument_html(
        cleaned,
        recipient_email=recipient_email or "unknown@example.com",
        subject=subject,
        campaign=campaign,
        prospect_source=prospect_source,
        recipient_name=recipient_name,
        track_clicks=track_clicks,
        track_opens=track_opens,
    )


def _inject_local(
    html: str,
    tracking_id: Optional[str],
    *,
    track_clicks: bool,
    track_opens: bool,
) -> tuple[str, str]:
    import uuid

    email_id = tracking_id or str(uuid.uuid4())
    base = _tracking_base()
    soup = BeautifulSoup(html or "", "html.parser")

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
            if any(m in href for m in _CLICK_MARKERS):
                continue
            link_id = str(uuid.uuid4())
            a["data-original-url"] = href
            a["href"] = f"{base}/.netlify/functions/click?id={link_id}"

    instrumented = str(soup)
    if track_opens and base:
        already = any(m in instrumented for m in _OPEN_MARKERS)
        if not already:
            pixel = (
                f'<img src="{base}/.netlify/functions/open?id={email_id}" '
                f'width="1" height="1" alt="" '
                f'style="display:none;width:1px;height:1px;border:0;opacity:0;" '
                f'data-tracking="open">'
            )
            if "</body>" in instrumented.lower():
                idx = instrumented.lower().rfind("</body>")
                instrumented = instrumented[:idx] + pixel + instrumented[idx:]
            else:
                instrumented = instrumented + pixel

    return instrumented, email_id


def html_for_preview(html: str) -> str:
    """Draft/UI preview without Netlify tracking URLs visible."""
    return strip_visible_tracking_urls(strip_tracking(html or ""))
