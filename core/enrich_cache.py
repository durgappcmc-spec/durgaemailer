# NOTE: Session + process cache for LinkedIn ZoomInfo enrichments (skip repeat ZI calls).
from __future__ import annotations

import json
import re
from typing import Any, Optional

_PROCESS_CACHE: dict[str, dict[str, Any]] = {}

_LINKEDIN_NORM_RE = re.compile(
    r"(?:https?://)?(?:(?:www|[a-z]{2})\.)?linkedin\.com/(in|company)/([^/?#\s\]\)]+)",
    re.I,
)


def normalize_linkedin_url(url: str) -> str:
    """Canonical cache key: https://www.linkedin.com/{in|company}/{slug} (no trailing slash)."""
    raw = (url or "").strip()
    m = _LINKEDIN_NORM_RE.search(raw)
    if not m:
        return raw.lower().rstrip("/")
    kind = (m.group(1) or "in").lower()
    slug = (m.group(2) or "").strip().strip("/")
    try:
        from urllib.parse import unquote

        slug = unquote(slug)
    except Exception:
        pass
    return f"https://www.linkedin.com/{kind}/{slug}".lower()


def _session_map() -> Optional[dict[str, Any]]:
    try:
        import streamlit as st

        if "enriched" not in st.session_state or not isinstance(
            st.session_state.get("enriched"), dict
        ):
            st.session_state["enriched"] = {}
        return st.session_state["enriched"]
    except Exception:
        return None


def get_cached_enrichment(url: str) -> Optional[dict[str, Any]]:
    key = normalize_linkedin_url(url)
    if not key:
        return None
    sess = _session_map()
    if sess and key in sess and isinstance(sess[key], dict):
        return sess[key]
    hit = _PROCESS_CACHE.get(key)
    return hit if isinstance(hit, dict) else None


def put_cached_enrichment(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    key = normalize_linkedin_url(url)
    data = dict(payload or {})
    data.setdefault("linkedin_url", key)
    if key:
        _PROCESS_CACHE[key] = data
        sess = _session_map()
        if sess is not None:
            sess[key] = data
        memory_write(data, url=key)
    return data


def memory_write(payload: dict[str, Any], *, url: str = "") -> None:
    """Persist an enrichment payload to RAG/JSONL memory."""
    try:
        from core import memory as mem

        name = str(payload.get("name") or "").strip() or "prospect"
        title = f"Enriched: {name}"
        text = json.dumps(payload, default=str, ensure_ascii=False)[:8000]
        source_id = (
            url
            or str(payload.get("linkedin_url") or "")
            or str(payload.get("email") or "")
            or str(payload.get("source_id") or "")
        )
        mem.add(
            text,
            source="enrichment",
            source_id=source_id or None,
            title=title,
            metadata={
                "email": payload.get("email") or "",
                "company": payload.get("company") or "",
                "linkedin_url": payload.get("linkedin_url") or url,
                "source_provider": payload.get("source") or "",
            },
        )
    except Exception:
        pass


def format_enrichment_panel(payload: dict[str, Any]) -> str:
    """User-facing enrichment card shown before any draft."""
    p = payload or {}
    name = str(p.get("name") or "").strip() or "—"
    title = str(p.get("title") or "").strip() or "—"
    company = str(p.get("company") or "").strip() or "—"
    email = str(p.get("email") or "").strip() or "not found"
    phone = str(p.get("phone") or "").strip() or "-"
    mobile = str(p.get("mobile") or "").strip() or "-"
    linkedin = str(p.get("linkedin_url") or "").strip() or "—"
    source = str(p.get("source") or "ZoomInfo").strip() or "ZoomInfo"
    if source.lower() == "zoominfo":
        source = "ZoomInfo"
    try:
        from connectors import prospect_location

        location = prospect_location(p) or "—"
    except Exception:
        location = str(p.get("location") or "").strip() or "—"
    lines = [
        f"Prospect: {name}, {title} at {company}",
        f"Email:    {email}",
        f"Phone:    {phone}",
        f"Mobile:   {mobile}",
        f"Location: {location}",
        f"LinkedIn: {linkedin}",
        f"Source:   {source}",
    ]
    extras = []
    if p.get("industry"):
        extras.append(f"Industry: {p.get('industry')}")
    if p.get("seniority"):
        extras.append(f"Seniority: {p.get('seniority')}")
    about = str(p.get("about") or "").strip()
    if about:
        extras.append(f"About: {about[:400]}" + ("…" if len(about) > 400 else ""))
    if extras:
        lines.append("")
        lines.extend(extras)
    return "\n".join(lines)


def format_enrichment_fields(payload: dict[str, Any]) -> str:
    """Labeled fields for the drafting prompt — never raw JSON."""
    p = payload or {}
    email = str(p.get("email") or "").strip() or "not found"
    phone = str(p.get("phone") or "").strip() or "-"
    mobile = str(p.get("mobile") or "").strip() or "-"
    try:
        from connectors import prospect_location

        prospect_loc = prospect_location(p)
    except Exception:
        prospect_loc = str(p.get("location") or "").strip()
    rows = [
        ("Full name", p.get("name") or ""),
        ("First name", p.get("first_name") or ""),
        ("Last name", p.get("last_name") or ""),
        ("Title", p.get("title") or ""),
        ("Company", p.get("company") or ""),
        ("Verified work email", email),
        ("Phone", phone),
        ("Mobile", mobile),
        ("Industry", p.get("industry") or ""),
        ("Location", prospect_loc),
        ("Seniority", p.get("seniority") or ""),
        ("LinkedIn", p.get("linkedin_url") or ""),
        ("About", p.get("about") or ""),
        ("Source", p.get("source") or "ZoomInfo"),
    ]
    return "\n".join(f"{k}: {v}" for k, v in rows if str(v).strip())


def email_not_found_prompt() -> str:
    return (
        "No verified work email was found. I will not invent an address.\n"
        "Reply with one of:\n"
        "(a) skip — do not draft\n"
        "(b) draft with a placeholder\n"
        "(c) try Apollo / RocketReach"
    )
