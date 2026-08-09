# NOTE: Break complex asks into: web-find orgs → ZoomInfo contacts → optional draft.
from __future__ import annotations

import json
import re
import sys
from typing import Any, Optional
from urllib.parse import urlparse

from connectors.prospects import search_all
from core.llm import extract_json, grounded_collect

_ORG_EXTRACT_SYSTEM = (
    "Extract organizations from research notes. Return JSON only matching the schema. "
    "Prefer real NGOs/nonprofits that match the user's mission filters. "
    "Omit companies that are clearly unrelated (banks, IT consultancies, e-commerce)."
)


def discover_orgs_from_web(
    user_msg: str,
    *,
    limit: int = 8,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], str]:
    """Google-grounded discovery of orgs, then structured extraction.

    Returns (orgs, sources, research_notes).
    Each org: name, website, location, focus, why_match.
    """
    research_prompt = f"""Research and list concrete organizations that match this request.

User request:
{user_msg}

Rules:
- Find real NGOs / nonprofits / foundations / trusts (names + websites when possible).
- If the user mentions girls / women 16+ / skilling / vocational training / livelihoods,
  prioritize orgs that actually run those programs (not generic "NGO" directories).
- Include city/region when known (e.g. Noida, Delhi NCR, India).
- Prefer 5–{limit} strong matches over a long weak list.
- Write a short bullet list with: Organization name — website — location — what they do for girls/skilling.
"""
    notes, sources = grounded_collect(
        research_prompt,
        system=(
            "You are a careful nonprofit researcher. Use Google Search. "
            "Only list organizations you can support from search results. "
            "Be specific; avoid inventing emails."
        ),
    )
    extract_prompt = f"""From these research notes, extract up to {limit} matching organizations.

Research notes:
{notes[:12000]}

Original user request:
{user_msg}

Return JSON:
{{
  "organizations": [
    {{
      "name": "Official org name",
      "website": "https://example.org or empty",
      "location": "City / region",
      "focus": "1-line program focus (girls skilling etc.)",
      "why_match": "Why it fits the user request"
    }}
  ]
}}
"""
    raw = extract_json(extract_prompt, system=_ORG_EXTRACT_SYSTEM, max_tokens=2500)
    orgs = _parse_orgs(raw)[:limit]
    return orgs, sources, notes


def zoominfo_contacts_for_orgs(
    orgs: list[dict[str, Any]],
    *,
    contacts_per_org: int = 3,
    titles: Optional[list[str]] = None,
    web_email_fallback: bool = True,
) -> list[dict[str, Any]]:
    """Look up each discovered org in ZoomInfo; return normalized contacts."""
    titles = titles or [
        "Founder",
        "Director",
        "CEO",
        "President",
        "Secretary",
        "Program Manager",
        "Head",
    ]
    prospects: list[dict[str, Any]] = []
    seen: set[str] = set()
    missing_email_orgs: list[dict[str, Any]] = []

    for org in orgs:
        name = (org.get("name") or "").strip()
        website = (org.get("website") or "").strip()
        domain = _domain_from_url(website)
        if not name and not domain:
            continue

        name_variants = _company_name_variants(name)
        matched: list[dict[str, Any]] = []

        # 1) Domain-only (most precise when ZoomInfo has the website)
        if domain:
            matched = _zi_search(
                {"company_domains": [domain]}, limit=contacts_per_org
            )

        # 2) Clean company name variants (no titles — broader recall)
        if not matched:
            for variant in name_variants:
                matched = _zi_search(
                    {
                        "company_names": [variant],
                        **({"locations": ["India"]} if org.get("location") else {}),
                    },
                    limit=contacts_per_org,
                )
                if matched:
                    break

        # 3) Name + leadership titles
        if not matched:
            for variant in name_variants[:2]:
                matched = _zi_search(
                    {
                        "company_names": [variant],
                        "titles": titles,
                    },
                    limit=contacts_per_org,
                )
                if matched:
                    break

        got_email = False
        for r in matched:
            email = (r.get("email") or "").strip().lower()
            key = email or f"{r.get('name')}|{r.get('company')}|{r.get('source_id')}"
            key = key.lower()
            if key in seen:
                continue
            seen.add(key)
            if email:
                got_email = True
            prospects.append(
                {
                    **r,
                    "org_focus": org.get("focus") or "",
                    "org_website": website,
                    "why_match": org.get("why_match") or "",
                    "company": r.get("company") or name,
                }
            )

        if not matched or not got_email:
            missing_email_orgs.append(org)
            if not matched:
                stub = {
                    "name": "",
                    "first_name": "",
                    "email": "",
                    "title": "",
                    "company": name,
                    "linkedin_url": "",
                    "location": org.get("location") or "",
                    "source": "web_research",
                    "source_id": domain or name,
                    "org_focus": org.get("focus") or "",
                    "org_website": website,
                    "why_match": org.get("why_match") or "",
                    "research_only": True,
                }
                key = f"org:{name.lower()}"
                if key not in seen:
                    seen.add(key)
                    prospects.append(stub)

    if web_email_fallback and missing_email_orgs:
        for hit in _web_find_org_emails(missing_email_orgs[:6]):
            email = (hit.get("email") or "").strip().lower()
            if not email or email in seen:
                continue
            seen.add(email)
            # Replace research_only stub for same company if present
            prospects = [
                p
                for p in prospects
                if not (
                    p.get("research_only")
                    and (p.get("company") or "").lower()
                    == (hit.get("company") or "").lower()
                )
            ]
            prospects.append(hit)

    prospects.sort(key=lambda p: (0 if (p.get("email") or "").strip() else 1))
    return prospects


def _web_find_org_emails(orgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """When ZoomInfo lacks emails, find public contact emails via Google Search."""
    if not orgs:
        return []
    listing = "\n".join(
        f"- {o.get('name')} | {o.get('website') or ''} | {o.get('location') or ''}"
        for o in orgs
    )
    prompt = f"""Find publicly listed contact or partnership emails for these NGOs/orgs.
Prefer info@, contact@, partnerships@, or named founder/director emails from official sites.

Organizations:
{listing}

Return a short note with org name and email only when you are confident.
"""
    notes, _sources = grounded_collect(
        prompt,
        system=(
            "Find public contact emails for nonprofits. "
            "Do not invent emails. If unsure, omit."
        ),
    )
    raw = extract_json(
        f"""Extract emails from these notes.

Notes:
{notes[:8000]}

Return JSON:
{{"contacts":[{{"company":"Org","name":"Person or empty","email":"a@b.org","title":"role or empty"}}]}}
Only include addresses that appear in the notes.
""",
        system="Return JSON only. Never invent email addresses.",
        max_tokens=1200,
    )
    try:
        data = json.loads(raw or "{}")
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    by_name = {(o.get("name") or "").strip().lower(): o for o in orgs}
    for row in data.get("contacts") or []:
        if not isinstance(row, dict):
            continue
        email = (row.get("email") or "").strip()
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            continue
        company = (row.get("company") or "").strip()
        org = by_name.get(company.lower()) or {}
        # fuzzy company match
        if not org:
            for n, o in by_name.items():
                if n and (n in company.lower() or company.lower() in n):
                    org = o
                    company = o.get("name") or company
                    break
        name = (row.get("name") or "").strip()
        first = name.split()[0] if name else "there"
        out.append(
            {
                "name": name or company,
                "first_name": first if name else "",
                "email": email,
                "title": (row.get("title") or "Partnerships").strip(),
                "company": company or org.get("name") or "",
                "linkedin_url": "",
                "location": org.get("location") or "",
                "source": "web_email",
                "source_id": email.lower(),
                "org_focus": org.get("focus") or "",
                "org_website": org.get("website") or "",
                "why_match": org.get("why_match") or "",
            }
        )
    return out


def _zi_search(query: dict[str, Any], *, limit: int) -> list[dict[str, Any]]:
    try:
        rows = search_all(query, providers=("zoominfo",), limit_per_provider=limit)
    except Exception as e:
        print(f"[research] zoominfo search error: {e}", file=sys.stderr)
        return []
    return [r for r in rows if not r.get("error")]


def _company_name_variants(name: str) -> list[str]:
    """Generate simpler ZoomInfo-friendly company name variants."""
    if not name:
        return []
    variants: list[str] = []
    clean = re.sub(r"\s*\([^)]*\)\s*", " ", name).strip()
    clean = re.sub(
        r"\b(NGO|Trust|Foundation|Society|Centre|Center)\b",
        " ",
        clean,
        flags=re.I,
    )
    clean = re.sub(r"\s+", " ", clean).strip(" -|,")
    for cand in (name, clean, clean.split(",")[0].strip()):
        if cand and cand not in variants:
            variants.append(cand)
    parts = [p for p in re.split(r"\s+", clean) if p]
    if parts:
        if parts[0] not in variants:
            variants.append(parts[0])
        if len(parts) >= 2:
            two = " ".join(parts[:2])
            if two not in variants:
                variants.append(two)
    return variants[:5]


def run_research_then_zoom(
    user_msg: str,
    *,
    org_limit: int = 8,
    contacts_per_org: int = 3,
) -> dict[str, Any]:
    """Full pipeline: web org discovery → ZoomInfo contact enrichment."""
    orgs, sources, notes = discover_orgs_from_web(user_msg, limit=org_limit)
    contacts = zoominfo_contacts_for_orgs(orgs, contacts_per_org=contacts_per_org)
    return {
        "organizations": orgs,
        "contacts": contacts,
        "sources": sources,
        "notes": notes,
    }


def wants_research_then_zoom(user_msg: str) -> bool:
    """True only for mission-fit org discovery → ZoomInfo (not CSR-as-sender drafts)."""
    from agent.intent import looks_like_mission_org_discovery

    return looks_like_mission_org_discovery(user_msg or "")


def _parse_orgs(raw: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(raw or "{}")
    except Exception:
        return []
    rows = data.get("organizations") or data.get("orgs") or data.get("results") or []
    out: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        name = (r.get("name") or r.get("organization") or "").strip()
        if not name:
            continue
        out.append(
            {
                "name": name,
                "website": (r.get("website") or r.get("url") or "").strip(),
                "location": (r.get("location") or r.get("city") or "").strip(),
                "focus": (r.get("focus") or r.get("programs") or "").strip(),
                "why_match": (r.get("why_match") or r.get("reason") or "").strip(),
            }
        )
    return out


def _domain_from_url(url: str) -> str:
    if not url:
        return ""
    text = url.strip()
    if not re.match(r"^https?://", text, re.I):
        text = "https://" + text
    try:
        host = urlparse(text).netloc.lower()
    except Exception:
        return ""
    host = host.split("@")[-1]
    if host.startswith("www."):
        host = host[4:]
    # Ignore social/link aggregators
    if any(
        x in host
        for x in (
            "linkedin.com",
            "facebook.com",
            "instagram.com",
            "twitter.com",
            "x.com",
            "youtube.com",
        )
    ):
        return ""
    return host
