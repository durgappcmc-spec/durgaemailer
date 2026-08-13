# NOTE: Google/DDG find CSR LinkedIn profiles, then ZoomInfo enrich (titles on ZI are often stale).
# Also: Google "CSR Head email {company}" when ZoomInfo has no CSR emails.
from __future__ import annotations

import json
import re
import sys
from typing import Any, Optional
from urllib.parse import unquote

import httpx

from connectors.zoominfo import (
    _CSR_TITLE_RE,
    _contact_relevance_key,
    extract_linkedin_url,
    names_from_linkedin_url,
)

_LINKEDIN_RE = re.compile(
    r"https?://(?:[\w.-]+\.)?linkedin\.com/in/([\w\-%]+)/?",
    re.I,
)
_EMAIL_RE = re.compile(
    r"\b([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})\b"
)
_GENERIC_EMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "yahoo.com",
        "yahoo.co.in",
        "hotmail.com",
        "outlook.com",
        "icloud.com",
        "protonmail.com",
        "aol.com",
        "live.com",
        "msn.com",
        "rediffmail.com",
    }
)


def discover_csr_via_web_then_zoominfo(
    *,
    company: str,
    domains: Optional[list[str]] = None,
    limit: int = 8,
    zi: Any = None,
) -> list[dict[str, Any]]:
    """Google/public web → LinkedIn URLs for CSR leaders → ZoomInfo enrich.

    ZoomInfo job titles are often wrong/stale; public LinkedIn + Google are
    used to find the right people first, then ZI fills email/mobile.
    Remaining slots are left for the caller to fill via normal ZI company search.
    """
    company = (company or "").strip()
    if not company or limit <= 0:
        return []

    web_hits = find_csr_linkedin_candidates(
        company=company,
        domains=domains or [],
        limit=max(limit, 5),
    )
    if not web_hits:
        print(f"[csr_web] no LinkedIn candidates for {company!r}", file=sys.stderr)
        return []

    if zi is None:
        try:
            from connectors.zoominfo import ZoomInfoConnector

            zi = ZoomInfoConnector()
        except Exception as e:
            print(f"[csr_web] ZoomInfo unavailable: {e}", file=sys.stderr)
            return [_web_hit_as_prospect(h, company) for h in web_hits[:limit]]

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for hit in web_hits:
        if len(out) >= limit:
            break
        li = (hit.get("linkedin_url") or "").strip()
        if not li:
            continue
        key = li.lower().rstrip("/")
        if key in seen:
            continue
        seen.add(key)

        enriched = None
        try:
            enriched = zi.enrich(
                {
                    "linkedin_url": li,
                    "company": company,
                    "first_name": hit.get("first_name"),
                    "last_name": hit.get("last_name"),
                }
            )
        except Exception as e:
            print(f"[csr_web] ZI enrich failed for {li}: {e}", file=sys.stderr)

        prospect = _merge_web_and_zi(hit, enriched, company=company)
        if not prospect:
            continue
        dedupe = (
            (prospect.get("email") or "").strip().lower()
            or key
            or (prospect.get("name") or "").strip().lower()
        )
        if dedupe in seen and dedupe != key:
            continue
        seen.add(dedupe)
        prospect["matched_on"] = (
            f"Google/LinkedIn → ZoomInfo ({hit.get('title') or 'CSR'})"
        )
        prospect["source"] = "zoominfo"
        out.append(prospect)
        print(
            f"[csr_web] enriched {prospect.get('name')} "
            f"<{prospect.get('email') or 'no-email'}> "
            f"title={prospect.get('title')!r}",
            file=sys.stderr,
        )

    out.sort(key=_contact_relevance_key)
    return out[:limit]


def discover_csr_emails_via_google(
    *,
    company: str,
    domains: Optional[list[str]] = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Google Search for public CSR Head / Head of CSR emails at a company.

    Used when ZoomInfo company/title search returned no CSR contacts with email.
    Never invents addresses — only emails that appear in grounded search notes.
    """
    company = (company or "").strip()
    if not company or limit <= 0:
        return []

    domains = [str(d).strip().lower() for d in (domains or []) if str(d).strip()]
    domain_hint = ", ".join(domains[:3]) if domains else "(official company domain)"

    try:
        from core.llm import extract_json, grounded_collect
    except Exception as e:
        print(f"[csr_web] email search unavailable: {e}", file=sys.stderr)
        return []

    prompt = f"""Search Google for CSR / Sustainability head emails at "{company}".

Try queries like:
- "{company}" "CSR Head" email
- "{company}" "Head of CSR" email
- "{company}" "Head CSR" email OR contact
- "{company}" CSR Sustainability email @{domain_hint if domains else 'company'}

Find publicly listed email addresses for people whose role is CSR, Head of CSR,
CSR & Sustainability, ESG, or Corporate Social Responsibility at {company}.
Prefer @{domain_hint} addresses when available.

Return a short note listing name, title, and email only when clearly published.
Do not invent emails.
"""
    try:
        notes, sources = grounded_collect(
            prompt,
            system=(
                "Find public CSR Head / Head of CSR emails via Google Search. "
                "Never invent email addresses. If unsure, omit."
            ),
        )
    except Exception as e:
        print(f"[csr_web] Google CSR email search failed: {e}", file=sys.stderr)
        return []

    blob_text = notes or ""
    for src in sources or []:
        blob_text += "\n" + str(src.get("url") or "") + " " + str(src.get("title") or "")

    raw = ""
    try:
        raw = extract_json(
            f"""Extract CSR contact emails from these Google Search notes for {company}.

Notes:
{blob_text[:9000]}

Return JSON:
{{"contacts":[{{"name":"Full Name or empty","title":"CSR Head or similar","email":"a@b.com","linkedin_url":""}}]}}

Rules:
- Only include emails that appear in the notes.
- Prefer CSR / Sustainability / ESG titles.
- Max {limit} contacts.
- Never invent emails.
""",
            system="Return JSON only. Never invent email addresses.",
            max_tokens=900,
        )
    except Exception as e:
        print(f"[csr_web] CSR email JSON extract failed: {e}", file=sys.stderr)

    contacts: list[dict[str, Any]] = []
    try:
        data = json.loads(raw or "{}") if raw else {}
    except Exception:
        data = _extract_json_object(raw or "") or {}
    if isinstance(data, dict):
        for row in data.get("contacts") or []:
            if not isinstance(row, dict):
                continue
            email = (row.get("email") or "").strip().lower()
            if not _is_plausible_csr_email(email, domains):
                continue
            title = (row.get("title") or "CSR Head").strip() or "CSR Head"
            name = (row.get("name") or "").strip()
            if name and "@" in name:
                name = ""
            first = name.split()[0] if name else ""
            contacts.append(
                {
                    "name": name,
                    "first_name": first,
                    "email": email,
                    "title": title,
                    "company": company,
                    "linkedin_url": _normalize_linkedin(
                        row.get("linkedin_url") or row.get("linkedin") or ""
                    ),
                    "location": "",
                    "source": "google_csr_email",
                    "source_id": email,
                    "matched_on": "Google CSR Head email",
                }
            )

    # Harvest any leftover emails from notes that look corporate
    seen = {(c.get("email") or "").lower() for c in contacts}
    for m in _EMAIL_RE.finditer(blob_text):
        if len(contacts) >= limit:
            break
        email = m.group(1).strip().lower()
        if email in seen or not _is_plausible_csr_email(email, domains):
            continue
        # Require CSR context near the email in notes
        start = max(0, m.start() - 80)
        end = min(len(blob_text), m.end() + 80)
        ctx = blob_text[start:end].lower()
        if not re.search(r"\bcsr\b|sustainab|esg\b|corporate\s+social", ctx):
            if domains and not any(email.endswith("@" + d) for d in domains):
                continue
        seen.add(email)
        contacts.append(
            {
                "name": "",
                "first_name": "",
                "email": email,
                "title": "CSR Head",
                "company": company,
                "linkedin_url": "",
                "location": "",
                "source": "google_csr_email",
                "source_id": email,
                "matched_on": "Google CSR Head email",
            }
        )

    print(
        f"[csr_web] Google CSR emails found {len(contacts)} for {company!r}",
        file=sys.stderr,
    )
    return contacts[:limit]


def _is_plausible_csr_email(email: str, domains: list[str]) -> bool:
    email = (email or "").strip().lower()
    if not email or not re.match(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$", email):
        return False
    try:
        from core.prospect_list import email_blocked_for_company_search

        if email_blocked_for_company_search(email):
            return False
    except Exception:
        pass
    domain = email.rsplit("@", 1)[-1]
    if domain in _GENERIC_EMAIL_DOMAINS:
        return False
    # If we know company domains, prefer them (but allow other corporate domains)
    if domains:
        if any(domain == d or domain.endswith("." + d) for d in domains):
            return True
        # Soft allow other non-generic domains (press releases sometimes use parent brand)
        return "." in domain and len(domain) > 4
    return "." in domain and len(domain) > 4


def find_csr_linkedin_candidates(
    *,
    company: str,
    domains: Optional[list[str]] = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Find CSR LinkedIn profiles via Gemini Google Search, then DDG fallback."""
    hits: list[dict[str, Any]] = []
    seen_li: set[str] = set()

    def _add(row: dict[str, Any]) -> None:
        li = _normalize_linkedin(row.get("linkedin_url") or "")
        if not li:
            return
        key = li.lower().rstrip("/")
        if key in seen_li:
            return
        seen_li.add(key)
        first, last = names_from_linkedin_url(li)
        name = (row.get("name") or "").strip() or f"{first} {last}".strip()
        hits.append(
            {
                "name": name,
                "title": (row.get("title") or "").strip(),
                "linkedin_url": li,
                "location": (row.get("location") or "").strip(),
                "first_name": first,
                "last_name": last,
                "discovery": row.get("discovery") or "web",
            }
        )

    for row in _google_gemini_csr_linkedin(company, limit=limit):
        _add(row)
        if len(hits) >= limit:
            return hits[:limit]

    for row in _ddg_csr_linkedin(
        company, domains=domains or [], limit=limit * 2
    ):
        _add(row)
        if len(hits) >= limit:
            break

    return hits[:limit]


def _google_gemini_csr_linkedin(company: str, limit: int = 8) -> list[dict[str, Any]]:
    """Use Gemini + Google Search grounding to list CSR leaders with LinkedIn URLs."""
    try:
        from core.llm import grounded_collect
    except Exception as e:
        print(f"[csr_web] grounded_collect unavailable: {e}", file=sys.stderr)
        return []

    prompt = f"""Search Google for the CSR / Sustainability / ESG / Corporate Social Responsibility
leaders at the company "{company}" (also try STL / Sterlite Technologies if that is the firm).

Return a JSON object ONLY (no markdown) with this shape:
{{"contacts":[
  {{"name":"Full Name","title":"Public job title","linkedin_url":"https://www.linkedin.com/in/slug","location":"City"}}
]}}

Rules:
- Prefer people whose public role includes CSR, Sustainability, ESG, or Corporate Social Responsibility.
- Only include real linkedin.com/in/ profile URLs.
- Max {limit} contacts, best matches first.
- If unsure of LinkedIn URL, omit that person.
"""
    try:
        text, sources = grounded_collect(
            prompt,
            system=(
                "You find CSR leaders and their LinkedIn profile URLs via Google Search. "
                "Respond with JSON only."
            ),
        )
    except Exception as e:
        print(f"[csr_web] Google Gemini search failed: {e}", file=sys.stderr)
        return []

    contacts: list[dict[str, Any]] = []
    # Parse JSON from model text
    blob = _extract_json_object(text or "")
    if isinstance(blob, dict):
        raw = blob.get("contacts") or blob.get("people") or []
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                li = _normalize_linkedin(
                    item.get("linkedin_url") or item.get("linkedin") or ""
                )
                if not li:
                    continue
                contacts.append(
                    {
                        "name": item.get("name") or "",
                        "title": item.get("title") or "",
                        "linkedin_url": li,
                        "location": item.get("location") or "",
                        "discovery": "google_gemini",
                    }
                )

    # Also harvest LinkedIn URLs from grounding sources / raw text
    for src in sources or []:
        li = _normalize_linkedin(src.get("url") or "")
        if li and not any(
            (c.get("linkedin_url") or "").lower().rstrip("/") == li.lower().rstrip("/")
            for c in contacts
        ):
            contacts.append(
                {
                    "name": src.get("title") or "",
                    "title": "CSR",
                    "linkedin_url": li,
                    "location": "",
                    "discovery": "google_grounding",
                }
            )
    for m in _LINKEDIN_RE.finditer(text or ""):
        li = _normalize_linkedin(m.group(0))
        if li and not any(
            (c.get("linkedin_url") or "").lower().rstrip("/") == li.lower().rstrip("/")
            for c in contacts
        ):
            contacts.append(
                {
                    "name": "",
                    "title": "CSR",
                    "linkedin_url": li,
                    "location": "",
                    "discovery": "google_text",
                }
            )

    print(
        f"[csr_web] Google Gemini found {len(contacts)} LinkedIn CSR candidates "
        f"for {company!r}",
        file=sys.stderr,
    )
    return contacts[:limit]


def _ddg_csr_linkedin(
    company: str,
    *,
    domains: list[str],
    limit: int = 10,
) -> list[dict[str, Any]]:
    """DuckDuckGo site:linkedin.com/in searches across CSR title phrases."""
    titles = [
        "Head CSR",
        "CSR Head",
        "Head of CSR",
        "CSR Sustainability",
        "Head of Sustainability",
        "ESG",
        "Corporate Social Responsibility",
    ]
    # Prefer company brand + domain hints in the query
    brand = company
    if domains:
        brand = f'{company} OR "{domains[0]}"'

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    headers = {"User-Agent": "DurgaEmailerBot/1.0"}
    try:
        with httpx.Client(timeout=12.0, follow_redirects=True, headers=headers) as client:
            for title in titles:
                if len(out) >= limit:
                    break
                q = f'site:linkedin.com/in "{company}" "{title}"'
                try:
                    r = client.get(
                        "https://html.duckduckgo.com/html/",
                        params={"q": q},
                    )
                except Exception:
                    continue
                if r.status_code >= 400:
                    continue
                html = r.text or ""
                for li in _linkedin_urls_from_html(html):
                    key = li.lower().rstrip("/")
                    if key in seen:
                        continue
                    seen.add(key)
                    first, last = names_from_linkedin_url(li)
                    out.append(
                        {
                            "name": f"{first} {last}".strip(),
                            "title": title,
                            "linkedin_url": li,
                            "location": "",
                            "first_name": first,
                            "last_name": last,
                            "discovery": "duckduckgo",
                        }
                    )
                    if len(out) >= limit:
                        break
                # Broader query without quoted title
                if len(out) < limit:
                    q2 = f"site:linkedin.com/in {brand} CSR OR Sustainability"
                    try:
                        r2 = client.get(
                            "https://html.duckduckgo.com/html/",
                            params={"q": q2},
                        )
                        if r2.status_code < 400:
                            for li in _linkedin_urls_from_html(r2.text or ""):
                                key = li.lower().rstrip("/")
                                if key in seen:
                                    continue
                                seen.add(key)
                                first, last = names_from_linkedin_url(li)
                                out.append(
                                    {
                                        "name": f"{first} {last}".strip(),
                                        "title": "CSR",
                                        "linkedin_url": li,
                                        "location": "",
                                        "first_name": first,
                                        "last_name": last,
                                        "discovery": "duckduckgo",
                                    }
                                )
                                if len(out) >= limit:
                                    break
                    except Exception:
                        pass
    except Exception as e:
        print(f"[csr_web] DDG search failed: {e}", file=sys.stderr)

    print(
        f"[csr_web] DDG found {len(out)} LinkedIn candidates for {company!r}",
        file=sys.stderr,
    )
    return out[:limit]


def _linkedin_urls_from_html(html: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r'uddg=([^&"]+)', html or ""):
        cand = unquote(m.group(1))
        li = _normalize_linkedin(cand.split("&")[0])
        if not li:
            continue
        key = li.lower().rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        urls.append(li)
    for m in _LINKEDIN_RE.finditer(html or ""):
        li = _normalize_linkedin(m.group(0))
        if not li:
            continue
        key = li.lower().rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        urls.append(li)
    return urls


def _normalize_linkedin(url: str) -> str:
    if not url:
        return ""
    # Prefer extract helper for cleanup
    got = extract_linkedin_url(url) or ""
    if got:
        return got
    m = _LINKEDIN_RE.search(url)
    if not m:
        return ""
    return f"https://www.linkedin.com/in/{m.group(1)}"


def _extract_json_object(text: str) -> Any:
    raw = (text or "").strip()
    if not raw:
        return None
    # Strip markdown fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except Exception:
        pass
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _web_hit_as_prospect(hit: dict[str, Any], company: str) -> dict[str, Any]:
    return {
        "name": hit.get("name") or "",
        "title": hit.get("title") or "",
        "company": company,
        "email": "",
        "linkedin_url": hit.get("linkedin_url") or "",
        "location": hit.get("location") or "",
        "source": "web_linkedin",
        "matched_on": f"Google/LinkedIn ({hit.get('discovery')})",
    }


def _merge_web_and_zi(
    web: dict[str, Any],
    zi_row: Optional[dict[str, Any]],
    *,
    company: str,
) -> Optional[dict[str, Any]]:
    web_title = (web.get("title") or "").strip()
    if not zi_row or zi_row.get("error"):
        # Keep LinkedIn-only row so the list still surfaces the right person
        p = _web_hit_as_prospect(web, company)
        return p if p.get("linkedin_url") else None

    prospect = dict(zi_row)
    prospect["linkedin_url"] = (
        prospect.get("linkedin_url") or web.get("linkedin_url") or ""
    )
    if not prospect.get("company"):
        prospect["company"] = company
    if web.get("location") and not prospect.get("location"):
        prospect["location"] = web.get("location")
    if web.get("name") and not prospect.get("name"):
        prospect["name"] = web.get("name")

    zi_title = (prospect.get("title") or "").strip()
    # Prefer Google/LinkedIn CSR title when ZoomInfo title is missing or non-CSR
    if web_title and _CSR_TITLE_RE.search(web_title):
        if not zi_title or not _CSR_TITLE_RE.search(zi_title):
            prospect["zi_title"] = zi_title
            prospect["title"] = web_title
            prospect["title_source"] = "google_linkedin"
    elif web_title and not zi_title:
        prospect["title"] = web_title
        prospect["title_source"] = "google_linkedin"

    return prospect
