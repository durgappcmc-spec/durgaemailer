# NOTE: Apollo mixed_people/search does not reveal emails by default; use enrich.
from __future__ import annotations

import sys
from typing import Any, Optional

import requests

from config import settings
from connectors import ProspectConnector, normalize


class ApolloConnector(ProspectConnector):
    name = "apollo"
    BASE = "https://api.apollo.io/api/v1"

    def __init__(self) -> None:
        self.api_key = settings.APOLLO_API_KEY

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
            "x-api-key": self.api_key,
        }

    def search(self, query: dict[str, Any], limit: int = 10) -> list[dict[str, Any]]:
        if not self.api_key:
            return [{"source": self.name, "error": "APOLLO_API_KEY not set"}]

        payload: dict[str, Any] = {
            "page": 1,
            "per_page": min(limit, 100),
        }
        if query.get("titles"):
            payload["person_titles"] = _as_list(query["titles"])
        if query.get("seniorities"):
            payload["person_seniorities"] = _as_list(query["seniorities"])
        if query.get("locations"):
            payload["person_locations"] = _as_list(query["locations"])
        if query.get("company_domains"):
            payload["q_organization_domains_list"] = _as_list(query["company_domains"])
        if query.get("company_names"):
            payload["organization_names"] = _as_list(query["company_names"])
        if query.get("keywords"):
            payload["q_keywords"] = (
                query["keywords"]
                if isinstance(query["keywords"], str)
                else ", ".join(_as_list(query["keywords"]))
            )
        if query.get("industries"):
            payload["organization_industry_tag_ids"] = _as_list(query["industries"])
        if query.get("employee_ranges"):
            payload["organization_num_employees_ranges"] = _as_list(
                query["employee_ranges"]
            )

        try:
            resp = requests.post(
                f"{self.BASE}/mixed_people/search",
                headers=self._headers(),
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"[apollo] search error: {e}", file=sys.stderr)
            return [{"source": self.name, "error": str(e)}]

        people = data.get("people") or data.get("contacts") or []
        results: list[dict[str, Any]] = []
        for p in people[:limit]:
            org = p.get("organization") or {}
            raw = {
                "id": p.get("id"),
                "first_name": p.get("first_name"),
                "last_name": p.get("last_name"),
                "name": p.get("name"),
                "email": p.get("email"),
                "phone": (p.get("phone_numbers") or [{}])[0].get("raw_number")
                if p.get("phone_numbers")
                else "",
                "title": p.get("title"),
                "company": org.get("name") or p.get("organization_name"),
                "linkedin_url": p.get("linkedin_url"),
                "location": ", ".join(
                    filter(None, [p.get("city"), p.get("state"), p.get("country")])
                ),
                "seniority": p.get("seniority"),
                "department": p.get("departments", [None])[0]
                if p.get("departments")
                else "",
            }
            results.append(normalize(raw, source=self.name, source_id=str(p.get("id") or "")))
        return results

    def enrich(self, identifier: dict[str, Any]) -> Optional[dict[str, Any]]:
        if not self.api_key:
            print("[apollo] enrich skipped: no API key", file=sys.stderr)
            return None

        payload: dict[str, Any] = {
            "reveal_personal_emails": identifier.get("reveal_personal_emails", False),
            "reveal_phone_number": identifier.get("reveal_phone_number", False),
        }
        for src, dst in [
            ("email", "email"),
            ("linkedin_url", "linkedin_url"),
            ("first_name", "first_name"),
            ("last_name", "last_name"),
            ("domain", "domain"),
            ("organization_name", "organization_name"),
            ("company", "organization_name"),
        ]:
            if identifier.get(src):
                payload[dst] = identifier[src]

        try:
            resp = requests.post(
                f"{self.BASE}/people/match",
                headers=self._headers(),
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"[apollo] enrich error: {e}", file=sys.stderr)
            return None

        p = data.get("person") or data
        if not p:
            return None
        org = p.get("organization") or {}
        raw = {
            "id": p.get("id"),
            "first_name": p.get("first_name"),
            "last_name": p.get("last_name"),
            "name": p.get("name"),
            "email": p.get("email"),
            "phone": (p.get("phone_numbers") or [{}])[0].get("raw_number")
            if p.get("phone_numbers")
            else "",
            "title": p.get("title"),
            "company": org.get("name") or p.get("organization_name"),
            "linkedin_url": p.get("linkedin_url"),
            "location": ", ".join(
                filter(None, [p.get("city"), p.get("state"), p.get("country")])
            ),
            "seniority": p.get("seniority"),
            "department": (p.get("departments") or [None])[0] or "",
        }
        return normalize(raw, source=self.name, source_id=str(p.get("id") or ""))


def _as_list(val: Any) -> list[str]:
    if val is None:
        return []
    if isinstance(val, list):
        return [str(v).strip() for v in val if str(v).strip()]
    return [p.strip() for p in str(val).split(",") if p.strip()]
