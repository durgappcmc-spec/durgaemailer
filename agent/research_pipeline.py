# NOTE: Break complex asks into: web-find orgs → ZoomInfo contacts → optional draft.
from __future__ import annotations

import json
import re
import sys
from typing import Any, Optional
from urllib.parse import urlparse

from connectors.prospects import search_all
from core.llm import extract_json, grounded_collect
from agent.limits import DEFAULT_CONTACTS_PER_ORG, DEFAULT_ORGS

_ORG_EXTRACT_SYSTEM = (
    "Extract organizations from research notes. Return JSON only matching the schema. "
    "Prefer real NGOs/nonprofits that match the user's mission filters. "
    "Omit companies that are clearly unrelated (banks, IT consultancies, e-commerce)."
)


def discover_orgs_from_web(
    user_msg: str,
    *,
    limit: int = DEFAULT_ORGS,
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
- Aim for up to {limit} strong matches (do not stop early at 5–10 if more fit).
- Write a bullet list with: Organization name — website — location — what they do for girls/skilling.
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
    raw = extract_json(
        extract_prompt,
        system=_ORG_EXTRACT_SYSTEM,
        max_tokens=min(8000, 800 + limit * 100),
    )
    orgs = _parse_orgs(raw)[:limit]
    return orgs, sources, notes


def enrich_one_org_on_zoominfo(
    org: dict[str, Any],
    *,
    contacts_per_org: int = DEFAULT_CONTACTS_PER_ORG,
    titles: Optional[list[str]] = None,
    web_email_fallback: bool = True,
) -> dict[str, Any]:
    """ZoomInfo lookup for a single NGO: dig for email + mobile per contact.

    Returns {org, contacts, with_email, with_mobile, notes}.
    """
    titles = titles or [
        "Founder",
        "Co-Founder",
        "Director",
        "Managing Director",
        "CEO",
        "President",
        "Secretary",
        "Trustee",
        "Program Manager",
        "Program Director",
        "Head",
        "Coordinator",
    ]
    name = (org.get("name") or "").strip()
    website = (org.get("website") or "").strip()
    domain = _domain_from_url(website)
    notes: list[str] = []
    matched: list[dict[str, Any]] = []

    if not name and not domain:
        return {
            "org": org,
            "contacts": [],
            "with_email": 0,
            "with_mobile": 0,
            "notes": ["skipped - no name/domain"],
        }

    # Reuse saved prospect list only when emails are already present
    try:
        from core.prospect_list import (
            find_by_company,
            save_prospects,
            saved_contacts_are_usable,
        )

        saved = find_by_company(name or domain, limit=contacts_per_org)
        if domain and not saved:
            saved = find_by_company(domain, limit=contacts_per_org)
        if saved and saved_contacts_are_usable(saved, min_with_email=1):
            notes.append(f"prospect list hit ({len(saved)} saved with email)")
            contacts = []
            seen: set[str] = set()
            for r in saved:
                email = (r.get("email") or "").strip().lower()
                key = email or f"{r.get('name')}|{r.get('company')}|{r.get('source_id')}"
                key = key.lower()
                if key in seen:
                    continue
                seen.add(key)
                contacts.append(
                    {
                        **r,
                        "org_focus": org.get("focus") or "",
                        "org_website": website,
                        "why_match": org.get("why_match") or "",
                        "company": r.get("company") or name,
                    }
                )
            contacts = contacts[:contacts_per_org]
            with_email = sum(1 for c in contacts if (c.get("email") or "").strip())
            with_mobile = sum(
                1
                for c in contacts
                if (c.get("mobile") or c.get("phone") or "").strip()
            )
            try:
                save_prospects([c for c in contacts if not c.get("research_only")])
            except Exception:
                pass
            return {
                "org": org,
                "contacts": contacts,
                "with_email": with_email,
                "with_mobile": with_mobile,
                "notes": notes,
            }
        if saved:
            notes.append(
                f"prospect list had {len(saved)} without email — auto ZoomInfo"
            )
    except Exception as e:
        notes.append(f"prospect list skip: {e}")

    name_variants = _company_name_variants(name)

    # 1) Domain
    if domain:
        matched = _zi_search({"company_domains": [domain]}, limit=contacts_per_org)
        if matched:
            notes.append(f"ZoomInfo domain hit ({domain})")

    # 2) Company name variants (+ India when location known)
    if not matched:
        for variant in name_variants:
            q: dict[str, Any] = {"company_names": [variant]}
            if org.get("location"):
                q["locations"] = ["India"]
            matched = _zi_search(q, limit=contacts_per_org)
            if matched:
                notes.append(f"ZoomInfo company hit ({variant})")
                break

    # 3) Name + leadership titles
    if not matched:
        for variant in name_variants[:3]:
            matched = _zi_search(
                {"company_names": [variant], "titles": titles},
                limit=contacts_per_org,
            )
            if matched:
                notes.append(f"ZoomInfo title hit ({variant})")
                break

    # Deepen: re-enrich each row to pull email + mobilePhone when missing
    matched = _deepen_email_and_mobile(matched)

    contacts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in matched:
        email = (r.get("email") or "").strip().lower()
        key = email or f"{r.get('name')}|{r.get('company')}|{r.get('source_id')}"
        key = key.lower()
        if key in seen:
            continue
        seen.add(key)
        contacts.append(
            {
                **r,
                "org_focus": org.get("focus") or "",
                "org_website": website,
                "why_match": org.get("why_match") or "",
                "company": r.get("company") or name,
            }
        )

    # Prefer people with email, then mobile
    contacts.sort(
        key=lambda p: (
            0 if (p.get("email") or "").strip() else 1,
            0 if (p.get("mobile") or p.get("phone") or "").strip() else 1,
        )
    )
    contacts = contacts[:contacts_per_org]

    with_email = sum(1 for c in contacts if (c.get("email") or "").strip())
    with_mobile = sum(
        1 for c in contacts if (c.get("mobile") or c.get("phone") or "").strip()
    )

    # Per-org public email fallback when ZoomInfo has no email
    if web_email_fallback and with_email == 0:
        notes.append("no ZoomInfo email — trying public web contacts")
        for hit in _web_find_org_emails([org]):
            email = (hit.get("email") or "").strip().lower()
            if not email or email in seen:
                continue
            seen.add(email)
            contacts.append(hit)
            with_email += 1

    if not contacts:
        notes.append("no ZoomInfo people — saved org stub")
        contacts.append(
            {
                "name": "",
                "first_name": "",
                "email": "",
                "phone": "",
                "mobile": "",
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
        )

    return {
        "org": org,
        "contacts": contacts,
        "with_email": with_email,
        "with_mobile": with_mobile,
        "notes": notes,
    }


def iter_enrich_orgs_on_zoominfo(
    orgs: list[dict[str, Any]],
    *,
    contacts_per_org: int = DEFAULT_CONTACTS_PER_ORG,
    web_email_fallback: bool = True,
    cancel_check: Optional[Any] = None,
):
    """Yield one NGO at a time after ZoomInfo email/mobile enrichment.

    Each yield: dict with keys type ('org'), index, total, result.
    """
    total = len(orgs)
    for i, org in enumerate(orgs, 1):
        if callable(cancel_check) and cancel_check():
            yield {"type": "cancelled", "index": i, "total": total}
            return
        result = enrich_one_org_on_zoominfo(
            org,
            contacts_per_org=contacts_per_org,
            web_email_fallback=web_email_fallback,
        )
        yield {
            "type": "org",
            "index": i,
            "total": total,
            "result": result,
        }


def zoominfo_contacts_for_orgs(
    orgs: list[dict[str, Any]],
    *,
    contacts_per_org: int = DEFAULT_CONTACTS_PER_ORG,
    titles: Optional[list[str]] = None,
    web_email_fallback: bool = True,
) -> list[dict[str, Any]]:
    """Look up each discovered org in ZoomInfo one-by-one; return contacts."""
    prospects: list[dict[str, Any]] = []
    for event in iter_enrich_orgs_on_zoominfo(
        orgs,
        contacts_per_org=contacts_per_org,
        web_email_fallback=web_email_fallback,
    ):
        if event.get("type") != "org":
            continue
        result = event.get("result") or {}
        prospects.extend(result.get("contacts") or [])
    prospects.sort(
        key=lambda p: (
            0 if (p.get("email") or "").strip() else 1,
            0 if (p.get("mobile") or p.get("phone") or "").strip() else 1,
        )
    )
    return prospects


def _deepen_email_and_mobile(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Re-enrich ZoomInfo person IDs when email or mobile is missing."""
    if not rows:
        return []
    try:
        from connectors.prospects import get_connector
        from connectors.zoominfo import _row_to_prospect

        zi = get_connector("zoominfo")
    except Exception as e:
        print(f"[research] zoominfo connector: {e}", file=sys.stderr)
        return rows

    need_ids: list[Any] = []
    for r in rows:
        pid = r.get("source_id") or r.get("id")
        if not pid:
            continue
        if (r.get("email") or "").strip() and (
            r.get("mobile") or r.get("phone") or ""
        ).strip():
            continue
        need_ids.append(pid)
    if not need_ids or not hasattr(zi, "_enrich_by_ids"):
        return rows

    enriched_by_id: dict[str, dict[str, Any]] = {}
    try:
        for i in range(0, len(need_ids), 10):
            for raw_row in zi._enrich_by_ids(need_ids[i : i + 10]):
                if not isinstance(raw_row, dict):
                    continue
                prospect = _row_to_prospect(raw_row)
                pid = str(
                    prospect.get("source_id")
                    or prospect.get("id")
                    or raw_row.get("personId")
                    or raw_row.get("id")
                    or ""
                )
                if pid:
                    enriched_by_id[pid] = prospect
    except Exception as e:
        print(f"[research] deepen enrich error: {e}", file=sys.stderr)
        return rows

    out: list[dict[str, Any]] = []
    for r in rows:
        pid = str(r.get("source_id") or r.get("id") or "")
        enr = enriched_by_id.get(pid)
        if not enr:
            out.append(r)
            continue
        merged = {**r}
        if (enr.get("email") or "").strip():
            merged["email"] = enr.get("email")
        if (enr.get("mobile") or "").strip():
            merged["mobile"] = enr.get("mobile")
        if (enr.get("phone") or "").strip():
            merged["phone"] = enr.get("phone")
        if (enr.get("linkedin_url") or "").strip() and not (
            merged.get("linkedin_url") or ""
        ).strip():
            merged["linkedin_url"] = enr.get("linkedin_url")
        out.append(merged)
    return out


def _web_find_org_emails(orgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """When ZoomInfo lacks emails, find public contact emails via Google Search."""
    if not orgs:
        return []
    listing = "\n".join(
        f"- {o.get('name')} | {o.get('website') or ''} | {o.get('location') or ''}"
        for o in orgs
    )
    prompt = f"""Find publicly listed contact or partnership emails AND phone/mobile numbers for these NGOs/orgs.
Prefer info@, contact@, partnerships@, or named founder/director emails from official sites.
Also note any published mobile/phone numbers for the same contacts.

Organizations:
{listing}

Return a short note with org name, email, and phone/mobile only when you are confident.
"""
    notes, _sources = grounded_collect(
        prompt,
        system=(
            "Find public contact emails and phones for nonprofits. "
            "Do not invent emails or phone numbers. If unsure, omit."
        ),
    )
    raw = extract_json(
        f"""Extract contacts from these notes.

Notes:
{notes[:8000]}

Return JSON:
{{"contacts":[{{"company":"Org","name":"Person or empty","email":"a@b.org","title":"role or empty","phone":"","mobile":""}}]}}
Only include emails/phones that appear in the notes.
""",
        system="Return JSON only. Never invent email addresses or phone numbers.",
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
        phone = (row.get("phone") or "").strip()
        mobile = (row.get("mobile") or "").strip()
        if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            continue
        if not email and not (phone or mobile):
            continue
        company = (row.get("company") or "").strip()
        org = by_name.get(company.lower()) or {}
        if not org:
            for n, o in by_name.items():
                if n and (n in company.lower() or company.lower() in n):
                    org = o
                    company = o.get("name") or company
                    break
        name = (row.get("name") or "").strip()
        if name and "@" in name:
            if not email and re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", name):
                email = name
            name = ""
        first = name.split()[0] if name else ""
        out.append(
            {
                "name": name,
                "first_name": first,
                "email": email,
                "phone": phone or mobile,
                "mobile": mobile or phone,
                "title": (row.get("title") or "Partnerships").strip(),
                "company": company or org.get("name") or "",
                "linkedin_url": "",
                "location": org.get("location") or "",
                "source": "web_email",
                "source_id": (email or f"{company}:{mobile or phone}").lower(),
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
    org_limit: int = DEFAULT_ORGS,
    contacts_per_org: int = DEFAULT_CONTACTS_PER_ORG,
) -> dict[str, Any]:
    """Full pipeline: web org discovery → ZoomInfo contact enrichment (one org at a time)."""
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
