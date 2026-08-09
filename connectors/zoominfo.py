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

        body: dict[str, Any] = {"rpp": min(max(int(limit), 1), 100), "page": 1}
        if query.get("first_name"):
            body["firstName"] = str(query["first_name"]).strip()
        if query.get("last_name"):
            body["lastName"] = str(query["last_name"]).strip()
        if query.get("full_name") or query.get("name"):
            body["fullName"] = str(query.get("full_name") or query.get("name")).strip()
        if query.get("titles"):
            body["jobTitle"] = _join(query["titles"])
        if query.get("company_names"):
            body["companyName"] = _join(query["company_names"])
        if query.get("company_domains"):
            body["companyWebsite"] = _join(query["company_domains"])
        if query.get("locations"):
            body["location"] = _join(query["locations"])
        if query.get("industries"):
            body["industryKeywords"] = _join(query["industries"])
        if query.get("seniorities"):
            body["managementLevel"] = _join(query["seniorities"])
        if query.get("department") or query.get("departments"):
            body["department"] = _join(
                query.get("department") or query.get("departments")
            )
        if query.get("keywords") and not body.get("jobTitle") and not body.get("fullName"):
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
                rows = self._enrich_by_ids(person_ids)
                by_id = {
                    str(c.get("personId") or c.get("id")): c for c in contacts[:limit]
                }
                for row in rows:
                    rid = str(row.get("id") or row.get("personId") or "")
                    search_hit = by_id.get(rid) or {}
                    if not _company_name(row) and search_hit:
                        row = {**row, "company": search_hit.get("company")}
                    if not row.get("jobTitle") and search_hit.get("jobTitle"):
                        row = {**row, "jobTitle": search_hit.get("jobTitle")}
                    prospect = _row_to_prospect(row)
                    if linkedin:
                        prospect["linkedin_url"] = linkedin
                    enriched.append(prospect)
            except Exception as e:
                print(f"[zoominfo] enrich-after-search error: {e}", file=sys.stderr)
                for c in contacts[:limit]:
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
