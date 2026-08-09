# NOTE: Shared prospect schema + ABC. Providers may omit fields; normalize fills defaults.
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional


def normalize(raw: dict[str, Any], source: str = "", source_id: str = "") -> dict[str, Any]:
    """Map provider-specific raw dict into the common prospect schema."""
    name = (
        raw.get("name")
        or " ".join(
            filter(
                None,
                [raw.get("first_name") or raw.get("firstName"), raw.get("last_name") or raw.get("lastName")],
            )
        ).strip()
        or ""
    )
    first = raw.get("first_name") or raw.get("firstName") or ""
    last = raw.get("last_name") or raw.get("lastName") or ""
    if not first and name:
        parts = name.split(None, 1)
        first = parts[0]
        last = parts[1] if len(parts) > 1 else last

    return {
        "name": name,
        "first_name": first,
        "last_name": last,
        "email": raw.get("email") or raw.get("email_address") or "",
        "phone": raw.get("phone")
        or raw.get("phone_number")
        or raw.get("mobile_phone")
        or raw.get("mobile")
        or "",
        "mobile": raw.get("mobile") or raw.get("mobile_phone") or raw.get("mobilePhone") or "",
        "title": raw.get("title") or raw.get("job_title") or raw.get("jobTitle") or "",
        "company": raw.get("company") or raw.get("organization_name") or raw.get("companyName") or "",
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
