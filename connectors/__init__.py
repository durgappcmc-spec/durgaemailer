# NOTE: Shared prospect schema + ABC. Providers may omit fields; normalize fills defaults.
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any, Optional

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$", re.I)


def looks_like_email(value: object) -> bool:
    return bool(_EMAIL_RE.match(str(value or "").strip()))


def humanize_email_local(email: str) -> str:
    """supriya.soni@x.com → Supriya Soni (never return the raw email)."""
    local = str(email or "").split("@")[0].strip()
    parts = [p for p in re.split(r"[._+\-]+", local) if p]
    return " ".join(p[:1].upper() + p[1:] for p in parts)


def sanitize_prospect(raw: dict[str, Any]) -> dict[str, Any]:
    """Ensure email stays in email; name is a person/org label, never an address."""
    p = dict(raw or {})
    name = str(p.get("name") or "").strip()
    email = str(p.get("email") or p.get("email_address") or "").strip()
    first = str(p.get("first_name") or p.get("firstName") or "").strip()
    last = str(p.get("last_name") or p.get("lastName") or "").strip()

    if looks_like_email(name):
        if not email:
            email = name
        name = ""
    if looks_like_email(first):
        if not email:
            email = first
        first = ""
    if looks_like_email(last):
        last = ""

    if not name:
        name = " ".join(x for x in (first, last) if x).strip()
    if not name and email:
        name = humanize_email_local(email)
        if not first:
            bits = name.split(None, 1)
            first = bits[0] if bits else ""
            last = bits[1] if len(bits) > 1 else last

    # Company must not be an email either
    company = str(
        p.get("company") or p.get("organization_name") or p.get("companyName") or ""
    ).strip()
    if looks_like_email(company):
        company = ""

    if not first and name and not looks_like_email(name):
        bits = name.split(None, 1)
        first = bits[0]
        if not last and len(bits) > 1:
            last = bits[1]

    p["name"] = name
    p["first_name"] = first
    p["last_name"] = last
    p["email"] = email
    p["company"] = company
    return p


def normalize(raw: dict[str, Any], source: str = "", source_id: str = "") -> dict[str, Any]:
    """Map provider-specific raw dict into the common prospect schema."""
    cleaned = sanitize_prospect(
        {
            "name": raw.get("name"),
            "first_name": raw.get("first_name") or raw.get("firstName"),
            "last_name": raw.get("last_name") or raw.get("lastName"),
            "email": raw.get("email") or raw.get("email_address"),
            "company": raw.get("company")
            or raw.get("organization_name")
            or raw.get("companyName"),
        }
    )
    name = cleaned["name"]
    first = cleaned["first_name"]
    last = cleaned["last_name"]

    return {
        "name": name,
        "first_name": first,
        "last_name": last,
        "email": cleaned["email"],
        "phone": raw.get("phone")
        or raw.get("phone_number")
        or raw.get("mobile_phone")
        or raw.get("mobile")
        or "",
        "mobile": raw.get("mobile") or raw.get("mobile_phone") or raw.get("mobilePhone") or "",
        "title": raw.get("title") or raw.get("job_title") or raw.get("jobTitle") or "",
        "company": cleaned["company"],
        "linkedin_url": raw.get("linkedin_url") or raw.get("linkedinUrl") or raw.get("linkedin") or "",
        "location": raw.get("location") or raw.get("city") or "",
        "seniority": raw.get("seniority") or raw.get("managementLevel") or "",
        "department": raw.get("department") or "",
        "source": source or raw.get("source") or "",
        "source_id": source_id or str(raw.get("id") or raw.get("source_id") or ""),
        "raw": raw,
    }


class ProspectConnector(ABC):
    """Abstract B2B prospect provider."""

    name: str = "base"

    @abstractmethod
    def search(self, query: dict[str, Any], limit: int = 10) -> list[dict[str, Any]]:
        """Search prospects. query keys are free-form filters."""
        ...

    @abstractmethod
    def enrich(self, identifier: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Enrich a single person. Returns normalized prospect or None."""
        ...
