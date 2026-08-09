# NOTE: JWT cached ~55 minutes (memory + disk). Prefer ZOOMINFO_API_KEY bearer;
# fall back to username/password then PKI. Never prompts the user interactively.
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

import requests

from config import _DATA, _ROOT, settings
from connectors import ProspectConnector, normalize

_JWT_CACHE = _DATA / "zoominfo_jwt.cache"
_LINKEDIN_RE = re.compile(
    r"https?://(?:[\w.-]+\.)?linkedin\.com/in/([\w\-%]+)/?",
    re.I,
)


class ZoomInfoConnector(ProspectConnector):
    name = "zoominfo"
    BASE = "https://api.zoominfo.com"
    OUTPUT_FIELDS = [
        "id",
        "firstName",
        "lastName",
        "email",
        "jobTitle",
        "companyName",
    ]

    def __init__(self) -> None:
        self.api_key = (
            (getattr(settings, "ZOOMINFO_API_KEY", "") or "").strip()
            or _load_api_key_file()
        )
        self.username = (settings.ZOOMINFO_USERNAME or "").strip()
        self.password = (settings.ZOOMINFO_PASSWORD or "").strip()
        self.client_id = (getattr(settings, "ZOOMINFO_CLIENT_ID", "") or "").strip()
        key_path = (getattr(settings, "ZOOMINFO_PRIVATE_KEY_PATH", "") or "").strip()
        self.private_key = _load_private_key(key_path)
        self.token: Optional[str] = None
        self.token_expires_at: float = 0.0

    def _configured(self) -> bool:
        if self.api_key:
            return True
        if self.username and self.password:
            return True
        if self.username and self.client_id and self.private_key:
            return True
        cached = _read_jwt_cache()
        if cached and cached.get("token") and time.time() < float(
            cached.get("expires_at") or 0
        ):
            return True
        return False

    def _authenticate(self) -> str:
        """Return a Bearer token from cache, password, API key, or PKI."""
        if self.token and time.time() < self.token_expires_at:
            return self.token

        # 1) Disk-cached JWT from a prior login
        cached = _read_jwt_cache()
        if cached and cached.get("token") and time.time() < float(
            cached.get("expires_at") or 0
        ):
            self.token = cached["token"]
            self.token_expires_at = float(cached["expires_at"])
            return self.token

        # 2) Username / password → JWT (silent durable auth; no user prompts)
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
                    self.token_expires_at = _jwt_expiry(token) or (time.time() + 55 * 60)
                    _write_jwt_cache(self.token, self.token_expires_at)
                    return self.token
            except Exception as e:
                print(f"[zoominfo] password auth failed: {e}", file=sys.stderr)

        # 3) Static API key / pre-issued JWT (skip if JWT already expired)
        if self.api_key and _token_usable(self.api_key):
            self.token = self.api_key
            self.token_expires_at = _jwt_expiry(self.api_key) or (
                time.time() + 24 * 60 * 60
            )
            return self.token

        # 4) PKI (private key file + client id)
        if self.username and self.client_id and self.private_key:
            try:
                import zi_api_auth_client

                token = zi_api_auth_client.pki_authentication(
                    self.username, self.client_id, self.private_key
                )
                if not token:
                    raise RuntimeError("PKI auth returned empty token")
                self.token = token
                self.token_expires_at = _jwt_expiry(token) or (time.time() + 55 * 60)
                _write_jwt_cache(self.token, self.token_expires_at)
                return self.token
            except Exception as e:
                print(f"[zoominfo] PKI auth failed: {e}", file=sys.stderr)
                raise

        raise RuntimeError(
            "ZoomInfo authentication failed. Set ZOOMINFO_USERNAME+ZOOMINFO_PASSWORD "
            "or a valid ZOOMINFO_API_KEY in .env (never prompted in chat)."
        )

    def _headers(self) -> dict[str, str]:
        token = self._authenticate()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def search(self, query: dict[str, Any], limit: int = 10) -> list[dict[str, Any]]:
        if not self._configured():
            return [{"source": self.name, "error": "ZoomInfo credentials not set"}]

        # LinkedIn URL → resolve via name(+company) search then enrich
        linkedin = (
            query.get("linkedin_url")
            or query.get("linkedin")
            or _first_linkedin(query.get("keywords") or "")
        )
        if linkedin:
            hit = self.enrich(
                {
                    "linkedin_url": linkedin,
                    "company": _join(query.get("company_names") or "") or None,
                    "first_name": query.get("first_name"),
                    "last_name": query.get("last_name"),
                }
            )
            return [hit] if hit else []

        limit = min(max(int(limit), 1), 100)
        results: list[dict[str, Any]] = []

        # NGO / nonprofit geo searches: company-first, then direct contact search.
        # (Free-text location/city fields 400 on this ZoomInfo tenant.)
        if _is_nonprofit_query(query):
            company_hits = self._search_companies(query, limit=min(limit, 15))
            if company_hits:
                results.extend(self._contacts_for_companies(company_hits, limit=limit))

        body = _build_contact_search_body(query, limit=limit)
        if not body and not results:
            return [
                {
                    "source": self.name,
                    "error": (
                        "No valid ZoomInfo filters. Use company/title/country "
                        "(not free-text city as 'location')."
                    ),
                }
            ]

        if body:
            try:
                resp = requests.post(
                    f"{self.BASE}/search/contact",
                    headers=self._headers(),
                    json=body,
                    timeout=45,
                )
                if resp.status_code == 400:
                    # Retry without any geo fields that may still be invalid
                    retry = {
                        k: v
                        for k, v in body.items()
                        if k
                        not in ("location", "city", "metroRegion", "state", "zipCode")
                    }
                    if "country" not in retry and _guess_country(query):
                        retry["country"] = _guess_country(query)
                    if len(retry) > 2:
                        resp = requests.post(
                            f"{self.BASE}/search/contact",
                            headers=self._headers(),
                            json=retry,
                            timeout=45,
                        )
                if resp.status_code >= 400 and not results:
                    detail = (resp.text or "")[:300]
                    return [
                        {
                            "source": self.name,
                            "error": f"{resp.status_code} from ZoomInfo: {detail}",
                        }
                    ]
                if resp.status_code < 400:
                    resp.raise_for_status()
                    data = resp.json()
                    contacts = data.get("data") or data.get("contacts") or []
                    contacts = sorted(
                        contacts,
                        key=lambda c: (
                            0 if c.get("hasEmail") else 1,
                            0 if c.get("hasSupplementalEmail") else 1,
                        ),
                    )
                    results.extend(self._enrich_contact_rows(contacts, limit=limit))
            except Exception as e:
                print(f"[zoominfo] search error: {e}", file=sys.stderr)
                if not results:
                    return [{"source": self.name, "error": str(e)}]

        # Dedupe; prefer rows that have email
        seen: set[str] = set()
        merged: list[dict[str, Any]] = []
        for p in sorted(
            results,
            key=lambda r: 0 if (r.get("email") or "").strip() else 1,
        ):
            key = (
                (p.get("email") or "").strip().lower()
                or str(p.get("source_id") or "")
                or (p.get("name") or "").strip().lower()
            )
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(p)
            if len(merged) >= limit:
                break
        return merged

    def _search_companies(
        self, query: dict[str, Any], limit: int = 10
    ) -> list[dict[str, Any]]:
        body: dict[str, Any] = {"rpp": min(max(int(limit), 1), 25), "page": 1}
        names = _join(query.get("company_names") or "")
        keywords = _join(query.get("keywords") or "")
        blob = f"{names} {keywords}".lower()
        if names and not re.search(r"\bngo\b|\bnonprofit\b|\bnon-profit\b", names, re.I):
            body["companyName"] = names
        else:
            body["companyName"] = "NGO"
        if re.search(r"foundation|trust|nonprofit|non-profit|ngo", blob):
            body["companyDescription"] = "NGO OR nonprofit OR foundation OR trust"
        geo = _geo_filters(query)
        body.update(geo)
        if query.get("company_domains"):
            body["companyWebsite"] = _join(query["company_domains"])
        try:
            resp = requests.post(
                f"{self.BASE}/search/company",
                headers=self._headers(),
                json=body,
                timeout=45,
            )
            if resp.status_code == 400:
                body.pop("zipCode", None)
                body.pop("city", None)
                body.pop("location", None)
                body.pop("state", None)
                if "country" not in body:
                    body["country"] = _guess_country(query) or "India"
                resp = requests.post(
                    f"{self.BASE}/search/company",
                    headers=self._headers(),
                    json=body,
                    timeout=45,
                )
            resp.raise_for_status()
            rows = (resp.json() or {}).get("data") or []
            # Prefer companies actually in the requested city when ZoomInfo returns city
            want_cities = {
                c
                for c in _CITY_ZIPS
                if re.search(rf"\b{re.escape(c)}\b", _query_blob(query))
            }
            if want_cities:
                local = [
                    r
                    for r in rows
                    if isinstance(r, dict)
                    and any(
                        city in str(r.get("city") or "").lower() for city in want_cities
                    )
                ]
                if local:
                    rows = local
            return [r for r in rows if isinstance(r, dict)]
        except Exception as e:
            print(f"[zoominfo] company search error: {e}", file=sys.stderr)
            return []

    def _contacts_for_companies(
        self, companies: list[dict[str, Any]], limit: int = 10
    ) -> list[dict[str, Any]]:
        """Pull contacts for company IDs, then enrich emails."""
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for co in companies:
            if len(out) >= limit:
                break
            cid = co.get("id") or co.get("companyId")
            if not cid:
                continue
            try:
                resp = requests.post(
                    f"{self.BASE}/search/contact",
                    headers=self._headers(),
                    json={
                        "companyId": str(cid),
                        "rpp": min(5, limit - len(out)),
                        "page": 1,
                    },
                    timeout=45,
                )
                resp.raise_for_status()
                rows = (resp.json() or {}).get("data") or []
            except Exception as e:
                print(f"[zoominfo] contacts-for-company error: {e}", file=sys.stderr)
                continue
            batch = self._enrich_contact_rows(rows, limit=limit - len(out))
            for p in batch:
                key = (p.get("email") or p.get("source_id") or p.get("name") or "").lower()
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                if not p.get("company"):
                    p["company"] = co.get("name") or ""
                if co.get("city") and not p.get("location"):
                    p["location"] = ", ".join(
                        filter(
                            None,
                            [co.get("city"), co.get("state"), co.get("country")],
                        )
                    )
                out.append(p)
        return out[:limit]

    def _enrich_contact_rows(
        self, contacts: list[dict[str, Any]], limit: int = 10
    ) -> list[dict[str, Any]]:
        ranked = sorted(
            contacts,
            key=lambda c: (
                0 if c.get("hasEmail") else 1,
                0 if c.get("hasSupplementalEmail") else 1,
            ),
        )
        person_ids = []
        for c in ranked[: max(limit, 10)]:
            pid = c.get("personId") or c.get("id")
            if pid:
                person_ids.append(pid)
        person_ids = person_ids[:limit]
        enriched: list[dict[str, Any]] = []
        if not person_ids:
            return enriched
        try:
            rows = self._enrich_by_ids(person_ids)
            by_id = {
                str(c.get("personId") or c.get("id")): c for c in ranked[:limit]
            }
            for row in rows:
                if row.get("errorMessage") or row.get("invalidInputFields"):
                    continue
                rid = str(row.get("id") or row.get("personId") or "")
                search_hit = by_id.get(rid) or {}
                if not _company_name(row) and search_hit:
                    row = {**row, "company": search_hit.get("company")}
                if not row.get("jobTitle") and search_hit.get("jobTitle"):
                    row = {**row, "jobTitle": search_hit.get("jobTitle")}
                prospect = _row_to_prospect(row)
                enriched.append(prospect)
        except Exception as e:
            print(f"[zoominfo] enrich-after-search error: {e}", file=sys.stderr)
            for c in ranked[:limit]:
                enriched.append(_row_to_prospect(c))
        return enriched

    def enrich(self, identifier: dict[str, Any]) -> Optional[dict[str, Any]]:
        if not self._configured():
            return None

        linkedin = (
            identifier.get("linkedin_url")
            or identifier.get("linkedin")
            or _first_linkedin(str(identifier.get("url") or ""))
        )
        first = (identifier.get("first_name") or identifier.get("firstName") or "").strip()
        last = (identifier.get("last_name") or identifier.get("lastName") or "").strip()
        company = (
            identifier.get("company")
            or identifier.get("companyName")
            or identifier.get("organization_name")
            or ""
        ).strip()
        if linkedin and (not first or not last):
            parsed_first, parsed_last = names_from_linkedin_url(linkedin)
            first = first or parsed_first
            last = last or parsed_last

        # Path A: LinkedIn → search by name(+company) → enrich by personId
        # (Many ZoomInfo tenants reject linkedInUrl as an enrich input.)
        if linkedin and first and last:
            try:
                search_body: dict[str, Any] = {
                    "firstName": first,
                    "lastName": last,
                    "rpp": 5,
                    "page": 1,
                }
                if company:
                    search_body["companyName"] = company
                resp = requests.post(
                    f"{self.BASE}/search/contact",
                    headers=self._headers(),
                    json=search_body,
                    timeout=45,
                )
                resp.raise_for_status()
                contacts = (resp.json() or {}).get("data") or []
                if contacts:
                    pid = contacts[0].get("personId") or contacts[0].get("id")
                    if pid:
                        rows = self._enrich_by_ids([pid])
                        if rows:
                            prospect = _row_to_prospect(rows[0])
                            prospect["linkedin_url"] = linkedin
                            if not prospect.get("company") and contacts[0].get("company"):
                                prospect["company"] = _company_name(contacts[0])
                            return prospect
            except Exception as e:
                print(f"[zoominfo] linkedin→search enrich error: {e}", file=sys.stderr)

        match_input: dict[str, Any] = {}
        if identifier.get("source_id") or identifier.get("personId"):
            match_input["personId"] = identifier.get("source_id") or identifier.get(
                "personId"
            )
        else:
            if identifier.get("email"):
                match_input["emailAddress"] = identifier["email"]
            if first:
                match_input["firstName"] = first
            if last:
                match_input["lastName"] = last
            if company:
                match_input["companyName"] = company
            # Try LinkedIn field when account supports it (ignored/invalid on some plans)
            if linkedin and not match_input.get("emailAddress") and not (
                first and last and company
            ):
                match_input["linkedInUrl"] = linkedin

        if not match_input:
            return None

        try:
            resp = requests.post(
                f"{self.BASE}/enrich/contact",
                headers=self._headers(),
                json={
                    "matchPersonInput": [match_input],
                    "outputFields": list(self.OUTPUT_FIELDS),
                },
                timeout=45,
            )
            resp.raise_for_status()
            rows = _flatten_enrich_rows(resp.json())
            if not rows:
                return None
            # Skip invalid-input error stubs
            if rows[0].get("errorMessage") or rows[0].get("invalidInputFields"):
                return None
            prospect = _row_to_prospect(rows[0])
            if linkedin:
                prospect["linkedin_url"] = linkedin
            return prospect
        except Exception as e:
            print(f"[zoominfo] enrich error: {e}", file=sys.stderr)
            return None

    def _enrich_by_ids(self, person_ids: list[Any]) -> list[dict[str, Any]]:
        enr = requests.post(
            f"{self.BASE}/enrich/contact",
            headers=self._headers(),
            json={
                "matchPersonInput": [{"personId": pid} for pid in person_ids],
                "outputFields": list(self.OUTPUT_FIELDS),
            },
            timeout=60,
        )
        enr.raise_for_status()
        return _flatten_enrich_rows(enr.json())


def extract_linkedin_url(text: str) -> str:
    """Return the first LinkedIn profile URL found in text, or ''."""
    return _first_linkedin(text or "")


def names_from_linkedin_url(url: str) -> tuple[str, str]:
    """Best-effort first/last from /in/slug (e.g. damian-lawlor → Damian, Lawlor)."""
    m = _LINKEDIN_RE.search(url or "")
    if not m:
        return "", ""
    slug = m.group(1)
    try:
        from urllib.parse import unquote

        slug = unquote(slug)
    except Exception:
        pass
    slug = slug.strip().strip("/")
    parts = [p for p in re.split(r"[-_]+", slug) if p]
    # Drop trailing LinkedIn id-looking segments (contain a digit), keep real name parts
    while len(parts) > 1 and re.search(r"\d", parts[-1]):
        parts.pop()
    parts = [p for p in parts if p and not p.isdigit()]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0].capitalize(), ""
    return parts[0].capitalize(), parts[-1].capitalize()


def _first_linkedin(text: Any) -> str:
    if not text:
        return ""
    m = _LINKEDIN_RE.search(str(text))
    if not m:
        return ""
    return f"https://www.linkedin.com/in/{m.group(1)}"


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


def _jwt_expiry(token: str) -> Optional[float]:
    """Return JWT exp as epoch seconds, or None if not a JWT / missing exp."""
    try:
        import base64

        parts = (token or "").split(".")
        if len(parts) < 2:
            return None
        pad = "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + pad))
        exp = payload.get("exp")
        return float(exp) if exp else None
    except Exception:
        return None


def _token_usable(token: str) -> bool:
    exp = _jwt_expiry(token)
    if exp is None:
        return bool(token)
    return time.time() < (exp - 60)


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


def _load_api_key_file() -> str:
    """Optional plain-text bearer/API key in credentials/zoominfo_api_key.txt."""
    for name in ("zoominfo_api_key.txt", "zoominfo_api.txt"):
        path = _ROOT / "credentials" / name
        try:
            if path.exists():
                text = path.read_text(encoding="utf-8").strip()
                if text and "BEGIN" not in text:
                    return text.splitlines()[0].strip()
        except Exception:
            continue
    return ""


def _read_jwt_cache() -> Optional[dict[str, Any]]:
    try:
        if not _JWT_CACHE.exists():
            return None
        data = json.loads(_JWT_CACHE.read_text(encoding="utf-8"))
        if data.get("token"):
            return data
    except Exception:
        return None
    return None


def _write_jwt_cache(token: str, expires_at: float) -> None:
    try:
        _DATA.mkdir(parents=True, exist_ok=True)
        _JWT_CACHE.write_text(
            json.dumps({"token": token, "expires_at": expires_at}),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"[zoominfo] jwt cache write error: {e}", file=sys.stderr)


def _join(val: Any) -> str:
    if isinstance(val, list):
        return ",".join(str(v).strip() for v in val if str(v).strip())
    return str(val).strip()


# India city → ZoomInfo zipCode (contact/company search rejects free-text location/city)
_CITY_ZIPS: dict[str, str] = {
    "noida": "201301,201304,201305,201306,201307,201309,201310,201318",
    "greater noida": "201308,201310",
    "delhi": "110001,110016,110017,110019,110024,110025,110048,110065",
    "new delhi": "110001,110016,110017,110019,110024",
    "gurgaon": "122001,122002,122003,122015,122016",
    "gurugram": "122001,122002,122003,122015,122016",
    "mumbai": "400001,400050,400051,400052,400053,400076",
    "bangalore": "560001,560025,560034,560066,560103",
    "bengaluru": "560001,560025,560034,560066,560103",
    "hyderabad": "500001,500032,500033,500034,500081",
    "chennai": "600001,600017,600028,600032,600113",
    "pune": "411001,411014,411045,411057",
}

_US_STATES = {
    "alabama",
    "alaska",
    "arizona",
    "arkansas",
    "california",
    "colorado",
    "connecticut",
    "delaware",
    "florida",
    "georgia",
    "hawaii",
    "idaho",
    "illinois",
    "indiana",
    "iowa",
    "kansas",
    "kentucky",
    "louisiana",
    "maine",
    "maryland",
    "massachusetts",
    "michigan",
    "minnesota",
    "mississippi",
    "missouri",
    "montana",
    "nebraska",
    "nevada",
    "new hampshire",
    "new jersey",
    "new mexico",
    "new york",
    "north carolina",
    "north dakota",
    "ohio",
    "oklahoma",
    "oregon",
    "pennsylvania",
    "rhode island",
    "south carolina",
    "south dakota",
    "tennessee",
    "texas",
    "utah",
    "vermont",
    "virginia",
    "washington",
    "west virginia",
    "wisconsin",
    "wyoming",
}


def _query_blob(query: dict[str, Any]) -> str:
    parts = [
        _join(query.get("locations") or ""),
        _join(query.get("keywords") or ""),
        _join(query.get("company_names") or ""),
        _join(query.get("industries") or ""),
        _join(query.get("titles") or ""),
        str(query.get("country") or ""),
    ]
    return " ".join(p for p in parts if p).lower()


def _is_nonprofit_query(query: dict[str, Any]) -> bool:
    blob = _query_blob(query)
    return bool(
        re.search(
            r"\bngo\b|\bnon[\s-]?profit\b|\bcharit\w*\b|\bfoundation\b|\btrust\b|"
            r"\bsocial\s+impact\b|\bngos\b",
            blob,
            re.I,
        )
    )


def _guess_country(query: dict[str, Any]) -> str:
    if query.get("country"):
        return str(query["country"]).strip()
    blob = _query_blob(query)
    for city in _CITY_ZIPS:
        if re.search(rf"\b{re.escape(city)}\b", blob):
            return "India"
    if re.search(r"\bindia\b|\bindian\b", blob):
        return "India"
    return ""


def _geo_filters(query: dict[str, Any]) -> dict[str, str]:
    """Map free-text locations to ZoomInfo-accepted geo fields.

    ZoomInfo rejects `location` and `city` on this tenant; use country / zipCode /
    US-Canada state instead.
    """
    out: dict[str, str] = {}
    locs = []
    raw = query.get("locations")
    if isinstance(raw, list):
        locs = [str(x).strip() for x in raw if str(x).strip()]
    elif raw:
        locs = [str(raw).strip()]
    blob = " ".join(locs).lower() + " " + _query_blob(query)

    zips: list[str] = []
    for city, z in _CITY_ZIPS.items():
        if re.search(rf"\b{re.escape(city)}\b", blob):
            zips.append(z)
    # Explicit zip codes in the query
    for m in re.findall(r"\b\d{5,6}\b", blob):
        zips.append(m)
    if zips:
        # Deduplicate while preserving order
        seen: set[str] = set()
        flat: list[str] = []
        for chunk in zips:
            for z in chunk.split(","):
                z = z.strip()
                if z and z not in seen:
                    seen.add(z)
                    flat.append(z)
        out["zipCode"] = ",".join(flat[:20])

    country = _guess_country(query)
    if country:
        out["country"] = country

    # US/Canada state only (ZoomInfo rejects Indian states here)
    for loc in locs:
        low = loc.lower().strip()
        if low in _US_STATES:
            out["state"] = loc.title()
            out.setdefault("country", "United States")
    return out


def _sanitize_keywords(val: Any) -> str:
    """Drop demographic / non-B2B phrases ZoomInfo cannot filter on."""
    text = _join(val)
    if not text:
        return ""
    # Remove age/gender beneficiary language so it doesn't become a fake jobTitle
    text = re.sub(
        r"\b(girls?|boys?|women|men|children|kids|age[sd]?\s*\d+\+?|\d+\+\s*years?"
        r"|above\s*\d+|under\s*\d+|years?\s*old)\b",
        " ",
        text,
        flags=re.I,
    )
    text = re.sub(r"\s+", " ", text).strip(" ,;")
    # NGO-only keyword left → handled via company/industry, not title
    if re.fullmatch(r"(ngo|ngos|nonprofit|non-profit|foundation|trust)s?", text, re.I):
        return ""
    return text


def _build_contact_search_body(query: dict[str, Any], limit: int = 10) -> dict[str, Any]:
    body: dict[str, Any] = {"rpp": min(max(int(limit), 1), 100), "page": 1}
    if query.get("first_name"):
        body["firstName"] = str(query["first_name"]).strip()
    if query.get("last_name"):
        body["lastName"] = str(query["last_name"]).strip()
    if query.get("full_name") or query.get("name"):
        body["fullName"] = str(query.get("full_name") or query.get("name")).strip()

    titles = _sanitize_keywords(query.get("titles") or "")
    if titles:
        body["jobTitle"] = titles

    names = _join(query.get("company_names") or "")
    if names:
        body["companyName"] = names
    elif _is_nonprofit_query(query):
        body["companyName"] = "NGO"
        body["companyDescription"] = "NGO OR nonprofit OR foundation OR trust"

    if query.get("company_domains"):
        body["companyWebsite"] = _join(query["company_domains"])

    # Never send free-text `location` / `city` — they 400 on this ZoomInfo account
    body.update(_geo_filters(query))

    industries = _join(query.get("industries") or "")
    if industries:
        body["industryKeywords"] = industries
    elif _is_nonprofit_query(query):
        body.setdefault("industryKeywords", "Nonprofit")

    if query.get("seniorities"):
        body["managementLevel"] = _join(query["seniorities"])
    if query.get("department") or query.get("departments"):
        body["department"] = _join(
            query.get("department") or query.get("departments")
        )

    kw = _sanitize_keywords(query.get("keywords") or "")
    if kw and not body.get("jobTitle") and not body.get("fullName"):
        # Only use as title when it looks like a role, not a topic dump
        if len(kw.split()) <= 6 and not _is_nonprofit_query({"keywords": kw}):
            body["jobTitle"] = kw

    # Must have at least one meaningful filter besides paging
    useful = {
        k
        for k in body
        if k not in ("rpp", "page") and body.get(k) not in (None, "", [])
    }
    if not useful:
        return {}
    return body
