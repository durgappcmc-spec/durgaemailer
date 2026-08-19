# NOTE: Strip/inject tracking helpers; wraps tracking.instrument without changing URL format.
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
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


_TID_COMMENT_RE = re.compile(
    r"<!--\s*relay-tid:([0-9a-fA-F\-]{8,})\s*-->",
    re.I,
)
_VISIBLE_TRACKING_URL_RE = re.compile(
    r"(?:&lt;|<)?https?://[^\s<>\"']*?"
    r"(?:durgaemailer-tracking\.netlify\.app|"
    r"/\.netlify/functions/(?:click|open)|/t/[co]/)"
    r"[^\s<>\"']*(?:&gt;|>)?",
    re.I,
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
    cm = _TID_COMMENT_RE.search(html or "")
    if cm:
        return cm.group(1)
    return None


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

    # Autolink text `<https://…netlify…>` is parsed as a bogus tag by html.parser
    for tag in list(soup.find_all(True)):
        name = (tag.name or "").lower()
        if name.startswith("http") and (
            "netlify" in str(tag).lower()
            or any(m in str(tag) for m in _OPEN_MARKERS + _CLICK_MARKERS)
        ):
            tag.decompose()

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

    For drafts, pass track_clicks=False and track_opens=False so Gmail/Relay
    preview cannot fire a live open pixel. Call prepare_draft_tracking() for
    drafts; inject the pixel only at send time.
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

    return instrument_html(
        cleaned,
        recipient_email=recipient_email or "unknown@example.com",
        subject=subject,
        campaign=campaign,
        prospect_source=prospect_source,
        recipient_name=recipient_name,
        track_clicks=track_clicks,
        track_opens=track_opens,
        email_id=existing or None,
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


def stamp_draft_tracking_id(html: str, tid: str) -> str:
    """Store tracking id in an HTML comment — Gmail will not fetch it as an open."""
    body = _TID_COMMENT_RE.sub("", html or "")
    tid = (tid or "").strip()
    if not tid:
        return body
    return f"{body.rstrip()}<!-- relay-tid:{tid} -->"


def prepare_draft_tracking(
    body_html: str,
    tracking_id: Optional[str] = None,
) -> tuple[str, str]:
    """Draft-safe: keep a tracking id, never a live open pixel or click wrapper."""
    import uuid

    existing = tracking_id or extract_tracking_id(body_html or "")
    cleaned = strip_tracking(body_html or "")
    tid = (existing or "").strip() or str(uuid.uuid4())
    return stamp_draft_tracking_id(cleaned, tid), tid


def html_for_preview(html: str) -> str:
    """Draft/UI preview without Netlify tracking URLs visible or fetchable."""
    raw = strip_visible_tracking_urls(html or "")
    return strip_visible_tracking_urls(strip_tracking(raw))


_PREFETCH_UA_RE = re.compile(
    r"GoogleImageProxy|ggpht\.com|googleusercontent|"
    r"YahooMailProxy|Googlebot|Google-PageRenderer|Google-Safety|"
    r"AdsBot|APIs-Google|Feedfetcher|GoogleAssociationService|"
    r"HeadlessChrome|Chrome-Lighthouse|"
    r"SafeLinks|safelinks\.protection|"
    r"Barracuda|Proofpoint|Mimecast|Symantec|MessageLabs|"
    r"\bbot\b|\bcrawler\b|\bspider\b|\bscanner\b|"
    r"curl/|wget/|python-requests|Go-http-client",
    re.I,
)


def is_bot_flag(val: Any) -> bool:
    """True for sheet/JSON bot flags (bool, 1, 'TRUE')."""
    if val is True or val == 1:
        return True
    return str(val or "").strip().lower() in {"true", "1", "yes"}


def is_prefetch_user_agent(ua: str | None) -> bool:
    """True for Gmail/Google image-proxy, scanners, and empty UAs — not a person click."""
    raw = (ua or "").strip()
    if not raw:
        return True
    return bool(_PREFETCH_UA_RE.search(raw))


def _parse_tracking_ts(val: Any) -> datetime | None:
    raw = str(val or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def filter_real_clicks(
    click_rows: list[dict[str, Any]] | None,
    *,
    send_rows: list[dict[str, Any]] | None = None,
    open_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Drop Gmail prefetch, scanners, and clicks recorded before the email was sent.

    Opening a Gmail draft, viewing Sent, or Gmail fetching a file/image proxy
    must not count as a recipient click. `open_rows` is accepted for call-site
    compatibility; prefetch is identified from user-agent / is_bot on the click.
    """
    del open_rows  # UA on the click row is the source of truth
    sends = {
        str(s.get("email_id") or "").strip(): s
        for s in (send_rows or [])
        if str(s.get("email_id") or "").strip()
    }

    kept: list[dict[str, Any]] = []
    for row in click_rows or []:
        if is_bot_flag(row.get("is_bot")) or is_prefetch_user_agent(
            str(row.get("user_agent") or "")
        ):
            continue
        eid = str(row.get("email_id") or "").strip()
        cts = _parse_tracking_ts(row.get("clicked_at"))
        send = sends.get(eid) or {}
        sent_at = _parse_tracking_ts(send.get("sent_at"))
        # Draft preview / Gmail file open before send
        if sent_at and cts and cts < sent_at - timedelta(seconds=15):
            continue
        kept.append(row)
    return kept
