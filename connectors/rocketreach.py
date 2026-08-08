# NOTE: Search returns teasers; lookup reveals contact details and spends credits.
from __future__ import annotations

import sys
from typing import Any, Optional

import requests

from config import settings
from connectors import ProspectConnector, normalize


class RocketReachConnector(ProspectConnector):
    name = "rocketreach"
    BASE = "https://api.rocketreach.co/api/v2"

    def __init__(self) -> None:
        self.api_key = settings.ROCKETREACH_API_KEY

    def _headers(self) -> dict[str, str]:
        return {
            "Api-Key": self.api_key,
            "Content-Type": "application/json",
        }

    def search(self, query: dict[str, Any], limit: int = 10) -> list[dict[str, Any]]:
        if not self.api_key:
            return [{"source": self.name, "error": "ROCKETREACH_API_KEY not set"}]

        q: dict[str, Any] = {}
        if query.get("titles"):
            q["current_title"] = _as_list(query["titles"])
        if query.get("company_names"):
            q["current_employer"] = _as_list(query["company_names"])
        if query.get("company_domains"):
            q["employer_domain"] = _as_list(query["company_domains"])
        if query.get("locations"):
            q["location"] = _as_list(query["locations"])
        if query.get("keywords"):
            q["keyword"] = _as_list(query["keywords"])
        if query.get("seniorities"):
            q["seniority"] = _as_list(query["seniorities"])

        try:
            resp = requests.post(
                f"{self.BASE}/person/search",
                headers=self._headers(),
                json={"query": q, "page_size": min(limit, 100)},
                timeout=45,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"[rocketreach] search error: {e}", file=sys.stderr)
            return [{"source": self.name, "error": str(e)}]

        profiles = data.get("profiles") or data.get("people") or []
        results: list[dict[str, Any]] = []
        for p in profiles[:limit]:
            pid = p.get("id")
            try:
                lookup = requests.get(
                    f"{self.BASE}/person/lookup",
                    headers=self._headers(),
                    params={"id": pid},
                    timeout=30,
                )
                lookup.raise_for_status()
                full = lookup.json()
                results.append(self._normalize_person(full))
            except Exception as e:
                print(f"[rocketreach] lookup fallback for {pid}: {e}", file=sys.stderr)
                results.append(self._normalize_person(p))
        return results

    def enrich(self, identifier: dict[str, Any]) -> Optional[dict[str, Any]]:
        if not self.api_key:
            return None
        params: dict[str, Any] = {}
        if identifier.get("source_id") or identifier.get("id"):
            params["id"] = identifier.get("source_id") or identifier.get("id")
        elif identifier.get("email"):
            params["email"] = identifier["email"]
        elif identifier.get("linkedin_url"):
            params["linkedin_url"] = identifier["linkedin_url"]
        else:
            if identifier.get("name"):
                params["name"] = identifier["name"]
            elif identifier.get("first_name") or identifier.get("last_name"):
                params["name"] = " ".join(
                    filter(
                        None,
                        [identifier.get("first_name"), identifier.get("last_name")],
                    )
                )
            if identifier.get("company"):
                params["current_employer"] = identifier["company"]

        try:
            resp = requests.get(
                f"{self.BASE}/person/lookup",
                headers=self._headers(),
                params=params,
                timeout=30,
            )
            resp.raise_for_status()
            return self._normalize_person(resp.json())
        except Exception as e:
            print(f"[rocketreach] enrich error: {e}", file=sys.stderr)
            return None

    def _normalize_person(self, p: dict[str, Any]) -> dict[str, Any]:
        emails = p.get("emails") or []
        email = ""
        if isinstance(emails, list) and emails:
            professional = [
                e
                for e in emails
                if isinstance(e, dict)
                and str(e.get("type", "")).lower() == "professional"
                and e.get("email")
            ]
            if professional:
                email = professional[0].get("email", "")
            else:
                first = emails[0]
                email = first.get("email", "") if isinstance(first, dict) else str(first)
        elif isinstance(emails, str):
            email = emails
        if not email:
            email = p.get("email") or ""

        phones = p.get("phones") or []
        phone = ""
        if isinstance(phones, list) and phones:
            first = phones[0]
            phone = first.get("number", "") if isinstance(first, dict) else str(first)
        else:
            phone = p.get("phone") or ""

        raw = {
            "id": p.get("id"),
            "name": p.get("name"),
            "first_name": p.get("first_name") or p.get("firstName"),
            "last_name": p.get("last_name") or p.get("lastName"),
            "email": email,
            "phone": phone,
            "title": p.get("current_title") or p.get("title"),
            "company": p.get("current_employer") or p.get("company"),
            "linkedin_url": p.get("linkedin_url") or p.get("linkedinUrl"),
            "location": p.get("location") or p.get("city"),
            "seniority": p.get("seniority"),
            "department": p.get("department"),
        }
        return normalize(raw, source=self.name, source_id=str(p.get("id") or ""))


def _as_list(val: Any) -> list[str]:
    if val is None:
        return []
    if isinstance(val, list):
        return [str(v).strip() for v in val if str(v).strip()]
    return [p.strip() for p in str(val).split(",") if p.strip()]
