# NOTE: JWT cached 55 minutes. _authenticate() is isolated for future PKI JWT swap.
from __future__ import annotations

import sys
import time
from typing import Any, Optional

import requests

from config import settings
from connectors import ProspectConnector, normalize


class ZoomInfoConnector(ProspectConnector):
    name = "zoominfo"
    BASE = "https://api.zoominfo.com"

    def __init__(self) -> None:
        self.username = settings.ZOOMINFO_USERNAME
        self.password = settings.ZOOMINFO_PASSWORD
        self.token: Optional[str] = None
        self.token_expires_at: float = 0.0

    def _authenticate(self) -> str:
        """Obtain JWT. Could later be swapped for PKI JWT signing."""
        if self.token and time.time() < self.token_expires_at:
            return self.token
        if not self.username or not self.password:
            raise RuntimeError("ZOOMINFO_USERNAME/PASSWORD not set")
        try:
            resp = requests.post(
                f"{self.BASE}/authenticate",
                json={"username": self.username, "password": self.password},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            self.token = data.get("jwt") or data.get("token") or ""
            # Cache for 55 minutes
            self.token_expires_at = time.time() + 55 * 60
            return self.token
        except Exception as e:
            print(f"[zoominfo] auth error: {e}", file=sys.stderr)
            raise

    def _headers(self) -> dict[str, str]:
        token = self._authenticate()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def search(self, query: dict[str, Any], limit: int = 10) -> list[dict[str, Any]]:
        if not self.username or not self.password:
            return [{"source": self.name, "error": "ZoomInfo credentials not set"}]

        body: dict[str, Any] = {"rpp": min(limit, 100), "page": 1}
        if query.get("titles"):
            body["jobTitle"] = _join(query["titles"])
        if query.get("company_names"):
            body["companyName"] = _join(query["company_names"])
        if query.get("company_domains"):
            body["companyWebsite"] = _join(query["company_domains"])
        if query.get("locations"):
            body["state"] = _join(query["locations"])
        if query.get("industries"):
            body["industry"] = _join(query["industries"])
        if query.get("seniorities"):
            body["managementLevel"] = _join(query["seniorities"])
        if query.get("department") or query.get("departments"):
            body["department"] = _join(
                query.get("department") or query.get("departments")
            )

        try:
            resp = requests.post(
                f"{self.BASE}/search/contact",
                headers=self._headers(),
                json=body,
                timeout=45,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"[zoominfo] search error: {e}", file=sys.stderr)
            return [{"source": self.name, "error": str(e)}]

        contacts = data.get("data") or data.get("contacts") or []
        person_ids = []
        for c in contacts[:limit]:
            pid = c.get("personId") or c.get("id")
            if pid:
                person_ids.append(pid)

        enriched: list[dict[str, Any]] = []
        if person_ids:
            try:
                enr = requests.post(
                    f"{self.BASE}/enrich/contact",
                    headers=self._headers(),
                    json={
                        "matchPersonInput": [{"personId": pid} for pid in person_ids],
                        "outputFields": [
                            "id",
                            "firstName",
                            "lastName",
                            "email",
                            "phone",
                            "jobTitle",
                            "companyName",
                            "linkedinUrl",
                            "city",
                            "state",
                            "country",
                            "managementLevel",
                            "department",
                        ],
                    },
                    timeout=45,
                )
                enr.raise_for_status()
                enr_data = enr.json()
                rows = enr_data.get("data") or enr_data.get("result") or []
                for row in rows:
                    raw = {
                        "id": row.get("id") or row.get("personId"),
                        "first_name": row.get("firstName"),
                        "last_name": row.get("lastName"),
                        "email": row.get("email"),
                        "phone": row.get("phone"),
                        "title": row.get("jobTitle"),
                        "company": row.get("companyName"),
                        "linkedin_url": row.get("linkedinUrl"),
                        "location": ", ".join(
                            filter(
                                None,
                                [row.get("city"), row.get("state"), row.get("country")],
                            )
                        ),
                        "seniority": row.get("managementLevel"),
                        "department": row.get("department"),
                    }
                    enriched.append(
                        normalize(
                            raw,
                            source=self.name,
                            source_id=str(raw.get("id") or ""),
                        )
                    )
            except Exception as e:
                print(f"[zoominfo] enrich-after-search error: {e}", file=sys.stderr)
                # Fall back to teaser search rows
                for c in contacts[:limit]:
                    raw = {
                        "id": c.get("personId") or c.get("id"),
                        "first_name": c.get("firstName"),
                        "last_name": c.get("lastName"),
                        "title": c.get("jobTitle"),
                        "company": c.get("companyName"),
                    }
                    enriched.append(
                        normalize(
                            raw,
                            source=self.name,
                            source_id=str(raw.get("id") or ""),
                        )
                    )
        return enriched

    def enrich(self, identifier: dict[str, Any]) -> Optional[dict[str, Any]]:
        if not self.username or not self.password:
            return None
        match_input: dict[str, Any] = {}
        if identifier.get("source_id") or identifier.get("personId"):
            match_input["personId"] = identifier.get("source_id") or identifier.get(
                "personId"
            )
        else:
            if identifier.get("email"):
                match_input["emailAddress"] = identifier["email"]
            if identifier.get("first_name"):
                match_input["firstName"] = identifier["first_name"]
            if identifier.get("last_name"):
                match_input["lastName"] = identifier["last_name"]
            if identifier.get("company"):
                match_input["companyName"] = identifier["company"]

        try:
            resp = requests.post(
                f"{self.BASE}/enrich/contact",
                headers=self._headers(),
                json={
                    "matchPersonInput": [match_input],
                    "outputFields": [
                        "id",
                        "firstName",
                        "lastName",
                        "email",
                        "phone",
                        "jobTitle",
                        "companyName",
                        "linkedinUrl",
                        "city",
                        "state",
                        "country",
                        "managementLevel",
                        "department",
                    ],
                },
                timeout=45,
            )
            resp.raise_for_status()
            data = resp.json()
            rows = data.get("data") or data.get("result") or []
            if not rows:
                return None
            row = rows[0]
            raw = {
                "id": row.get("id") or row.get("personId"),
                "first_name": row.get("firstName"),
                "last_name": row.get("lastName"),
                "email": row.get("email"),
                "phone": row.get("phone"),
                "title": row.get("jobTitle"),
                "company": row.get("companyName"),
                "linkedin_url": row.get("linkedinUrl"),
                "location": ", ".join(
                    filter(None, [row.get("city"), row.get("state"), row.get("country")])
                ),
                "seniority": row.get("managementLevel"),
                "department": row.get("department"),
            }
            return normalize(raw, source=self.name, source_id=str(raw.get("id") or ""))
        except Exception as e:
            print(f"[zoominfo] enrich error: {e}", file=sys.stderr)
            return None


def _join(val: Any) -> str:
    if isinstance(val, list):
        return ",".join(str(v).strip() for v in val if str(v).strip())
    return str(val).strip()
