# NOTE: JWT cached ~55 minutes. Supports username/password and optional PKI key file.
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Optional

import requests

from config import _ROOT, settings
from connectors import ProspectConnector, normalize


class ZoomInfoConnector(ProspectConnector):
    name = "zoominfo"
    BASE = "https://api.zoominfo.com"

    def __init__(self) -> None:
        self.username = (settings.ZOOMINFO_USERNAME or "").strip()
        self.password = (settings.ZOOMINFO_PASSWORD or "").strip()
        self.client_id = (getattr(settings, "ZOOMINFO_CLIENT_ID", "") or "").strip()
        key_path = (getattr(settings, "ZOOMINFO_PRIVATE_KEY_PATH", "") or "").strip()
        self.private_key = _load_private_key(key_path)
        self.token: Optional[str] = None
        self.token_expires_at: float = 0.0

    def _configured(self) -> bool:
        if self.username and self.password:
            return True
        if self.username and self.client_id and self.private_key:
            return True
        return False

    def _authenticate(self) -> str:
        """Obtain JWT via password, then optional PKI private key."""
        if self.token and time.time() < self.token_expires_at:
            return self.token
        if not self._configured():
            raise RuntimeError(
                "ZoomInfo credentials not set. Set ZOOMINFO_USERNAME + ZOOMINFO_PASSWORD "
                "and/or ZOOMINFO_CLIENT_ID + credentials/zoominfo.txt private key."
            )

        # 1) Username / password (preferred when available)
        if self.username and self.password:
            try:
                resp = requests.post(
                    f"{self.BASE}/authenticate",
                    json={"username": self.username, "password": self.password},
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()
                token = data.get("jwt") or data.get("token") or ""
                if token:
                    self.token = token
                    self.token_expires_at = time.time() + 55 * 60
                    return self.token
            except Exception as e:
                print(f"[zoominfo] password auth failed: {e}", file=sys.stderr)
                if not (self.client_id and self.private_key):
                    raise

        # 2) PKI (private key file + client id)
        if self.username and self.client_id and self.private_key:
            try:
                import zi_api_auth_client

                token = zi_api_auth_client.pki_authentication(
                    self.username, self.client_id, self.private_key
                )
                if not token:
                    raise RuntimeError("PKI auth returned empty token")
                self.token = token
                self.token_expires_at = time.time() + 55 * 60
                return self.token
            except Exception as e:
                print(f"[zoominfo] PKI auth failed: {e}", file=sys.stderr)
                raise

        raise RuntimeError("ZoomInfo authentication failed")

    def _headers(self) -> dict[str, str]:
        token = self._authenticate()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def search(self, query: dict[str, Any], limit: int = 10) -> list[dict[str, Any]]:
        if not self._configured():
            return [{"source": self.name, "error": "ZoomInfo credentials not set"}]

        body: dict[str, Any] = {"rpp": min(max(int(limit), 1), 100), "page": 1}
        if query.get("titles"):
            body["jobTitle"] = _join(query["titles"])
        if query.get("company_names"):
            body["companyName"] = _join(query["company_names"])
        if query.get("company_domains"):
            body["companyWebsite"] = _join(query["company_domains"])
        if query.get("locations"):
            # ZoomInfo accepts state / country / metroRegion depending on value
            body["location"] = _join(query["locations"])
        if query.get("industries"):
            body["industryKeywords"] = _join(query["industries"])
        if query.get("seniorities"):
            body["managementLevel"] = _join(query["seniorities"])
        if query.get("department") or query.get("departments"):
            body["department"] = _join(
                query.get("department") or query.get("departments")
            )
        if query.get("keywords"):
            # Free-text fallback into job title when no structured titles given
            if not body.get("jobTitle"):
                body["jobTitle"] = _join(query["keywords"])

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
                            "jobTitle",
                            "companyName",
                        ],
                    },
                    timeout=60,
                )
                enr.raise_for_status()
                rows = _flatten_enrich_rows(enr.json())
                by_id = {
                    str(c.get("personId") or c.get("id")): c for c in contacts[:limit]
                }
                for row in rows:
                    rid = str(row.get("id") or row.get("personId") or "")
                    search_hit = by_id.get(rid) or {}
                    # Preserve nested company from search when enrich omits it
                    if not _company_name(row) and search_hit:
                        row = {**row, "company": search_hit.get("company")}
                    if not row.get("jobTitle") and search_hit.get("jobTitle"):
                        row = {**row, "jobTitle": search_hit.get("jobTitle")}
                    enriched.append(_row_to_prospect(row))
            except Exception as e:
                print(f"[zoominfo] enrich-after-search error: {e}", file=sys.stderr)
                for c in contacts[:limit]:
                    enriched.append(_row_to_prospect(c))
        return enriched

    def enrich(self, identifier: dict[str, Any]) -> Optional[dict[str, Any]]:
        if not self._configured():
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
                        "jobTitle",
                        "companyName",
                    ],
                },
                timeout=45,
            )
            resp.raise_for_status()
            rows = _flatten_enrich_rows(resp.json())
            if not rows:
                return None
            return _row_to_prospect(rows[0])
        except Exception as e:
            print(f"[zoominfo] enrich error: {e}", file=sys.stderr)
            return None


def _flatten_enrich_rows(enr_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize ZoomInfo enrich payloads into a flat list of contact dicts."""
    payload = enr_data.get("data") if isinstance(enr_data, dict) else None
    rows: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        for block in payload.get("result") or []:
            if isinstance(block, dict):
                rows.extend(block.get("data") or [])
        if not rows and payload.get("data"):
            maybe = payload.get("data")
            if isinstance(maybe, list):
                rows.extend(maybe)
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict) and "data" in item:
                rows.extend(item.get("data") or [])
            elif isinstance(item, dict):
                rows.append(item)
    elif isinstance(enr_data.get("result"), list):
        for block in enr_data["result"]:
            if isinstance(block, dict):
                rows.extend(block.get("data") or [])
    return [r for r in rows if isinstance(r, dict)]


def _company_name(row: dict[str, Any]) -> str:
    """ZoomInfo may return companyName or nested company.name."""
    direct = row.get("companyName") or row.get("company")
    if isinstance(direct, dict):
        return str(direct.get("name") or "").strip()
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    nested = row.get("company")
    if isinstance(nested, dict):
        return str(nested.get("name") or "").strip()
    return ""


def _row_to_prospect(row: dict[str, Any]) -> dict[str, Any]:
    raw = {
        "id": row.get("id") or row.get("personId"),
        "first_name": row.get("firstName"),
        "last_name": row.get("lastName"),
        "email": row.get("email"),
        "phone": row.get("phone"),
        "title": row.get("jobTitle"),
        "company": _company_name(row),
        "linkedin_url": row.get("linkedinUrl"),
        "location": ", ".join(
            filter(None, [row.get("city"), row.get("state"), row.get("country")])
        ),
        "seniority": row.get("managementLevel"),
        "department": row.get("department"),
    }
    return normalize(raw, source="zoominfo", source_id=str(raw.get("id") or ""))


def _load_private_key(path: str) -> str:
    if not path:
        candidate = _ROOT / "credentials" / "zoominfo.txt"
    else:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = _ROOT / candidate
    try:
        if candidate.exists():
            text = candidate.read_text(encoding="utf-8").strip()
            if "BEGIN" in text and "PRIVATE KEY" in text:
                return text
    except Exception as e:
        print(f"[zoominfo] could not read private key: {e}", file=sys.stderr)
    return ""


def _join(val: Any) -> str:
    if isinstance(val, list):
        return ",".join(str(v).strip() for v in val if str(v).strip())
    return str(val).strip()
