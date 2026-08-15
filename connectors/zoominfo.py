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
# Canonical detector (protocol required) plus protocol-optional paste-safe variant.
# Country hosts like in.linkedin.com / uk.linkedin.com must match too.
_LINKEDIN_CANON_RE = re.compile(
    r"https?://(?:(?:www|[a-z]{2})\.)?linkedin\.com/(in|company)/[^\s\]\)]+",
    re.I,
)
_LINKEDIN_RE = re.compile(
    r"(?:https?://)?(?:(?:www|[a-z]{2})\.)?linkedin\.com/(in|company)/([^\s\]\)]+)",
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
        "phone",
        "mobilePhone",
        "externalUrls",
        "city",
        "state",
        "country",
        "street",
        "zipCode",
        "managementLevel",
        "department",
        "companyIndustry",
        "bio",
    ]
    OUTPUT_FIELDS_MIN = [
        "id",
        "firstName",
        "lastName",
        "email",
        "jobTitle",
        "companyName",
        "phone",
        "mobilePhone",
        "externalUrls",
        "city",
        "state",
        "country",
        "street",
        "zipCode",
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

    def health_check(self) -> dict[str, Any]:
        """Return {ok: bool, detail: str} for sidebar status dot."""
        if not self._configured():
            return {"ok": False, "detail": "credentials not set"}
        try:
            self._authenticate()
            return {"ok": True, "detail": "authenticated"}
        except Exception as e:
            return {"ok": False, "detail": str(e)[:200]}

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

        # Expand short aliases ("sterlite tech" → "Sterlite Technologies") so
        # company + contact search hit the right firm.
        query = _with_company_name_aliases(query)

        cn_list = query.get("company_names") or []
        if isinstance(cn_list, list) and cn_list:
            rank_needle = str(cn_list[0])
            names = _join(cn_list)
        else:
            names = _join(cn_list)
            rank_needle = names

        company_hits: list[dict[str, Any]] = []
        cascade_titles: list[str] = []
        expand = True
        domain_list: list[str] = []

        # 1) ZoomInfo company-first CSR title cascade FIRST (emails from ZI).
        #    Do not expand to non-CSR yet — keep slots for CSR email fallbacks.
        if names or _is_nonprofit_query(query):
            company_hits = self._search_companies(query, limit=min(15, max(limit, 5)))
            company_hits = _rank_companies_for_query(company_hits, rank_needle)
            cascade_titles, expand = _title_cascade_for_query(query)
            domains = query.get("company_domains") or query.get("domains") or []
            if isinstance(domains, str):
                domains = [domains] if domains.strip() else []
            domain_list = [str(d) for d in domains if d]
            if company_hits or domain_list:
                results.extend(
                    self._contacts_for_companies(
                        company_hits[:5] if company_hits else [],
                        limit=limit,
                        titles=cascade_titles,
                        expand=False,
                        domains=domain_list,
                    )
                )
                print(
                    f"[zoominfo] company-first CSR: {len(company_hits)} firms → "
                    f"{len(results)} contacts (q={rank_needle!r}, "
                    f"titles={cascade_titles[:3]!r}"
                    f"{'…' if len(cascade_titles) > 3 else ''}, "
                    f"domains={domain_list!r})",
                    file=sys.stderr,
                )

        def _csr_with_email(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [
                p
                for p in rows
                if (p.get("email") or "").strip()
                and _CSR_TITLE_RE.search(str(p.get("title") or p.get("jobTitle") or ""))
            ]

        # Keep emailed CSR when available (free slots from email-less CSR stubs)
        if _csr_with_email(results):
            results = [
                p
                for p in results
                if (p.get("email") or "").strip()
                or not _CSR_TITLE_RE.search(
                    str(p.get("title") or p.get("jobTitle") or "")
                )
            ]

        # 2) If ZoomInfo found no CSR emails → Google "CSR Head email {company}"
        #    plus LinkedIn → ZoomInfo enrich (public titles are often better).
        if (
            names
            and not _csr_with_email(results)
            and not _is_nonprofit_query(query)
            and not query.get("skip_web_csr")
            and not (query.get("linkedin_url") or query.get("linkedin"))
        ):
            company_label = rank_needle or names.split(",")[0]
            remaining = max(limit - len(results), 3)
            try:
                from agent.csr_web_discovery import (
                    discover_csr_emails_via_google,
                    discover_csr_via_web_then_zoominfo,
                )

                email_hits = discover_csr_emails_via_google(
                    company=company_label,
                    domains=domain_list,
                    limit=min(remaining, 5),
                )
                if email_hits:
                    results.extend(email_hits)
                    print(
                        f"[zoominfo] Google CSR emails: +{len(email_hits)} "
                        f"for {company_label!r}",
                        file=sys.stderr,
                    )

                if not _csr_with_email(results):
                    web_n = min(max(remaining, 3), limit)
                    web_hits = discover_csr_via_web_then_zoominfo(
                        company=company_label,
                        domains=domain_list,
                        limit=web_n,
                        zi=self,
                    )
                    # Prefer enriched rows that actually have email
                    with_mail = [h for h in web_hits if (h.get("email") or "").strip()]
                    add = with_mail or web_hits
                    if add:
                        results.extend(add)
                        print(
                            f"[zoominfo] Google→LI→ZI CSR: +{len(add)} "
                            f"({len(with_mail)} with email) for {company_label!r}",
                            file=sys.stderr,
                        )
            except Exception as e:
                print(f"[zoominfo] Google CSR fallback skipped: {e}", file=sys.stderr)

        # 3) Still under limit → broaden to other contacts at the firm
        if (
            expand
            and (company_hits or domain_list)
            and len(results) < limit
        ):
            remaining = limit - len(results)
            results.extend(
                self._contacts_for_companies(
                    company_hits[:5] if company_hits else [],
                    limit=remaining,
                    titles=[],
                    expand=True,
                    domains=domain_list,
                )
            )
            print(
                f"[zoominfo] expand non-CSR: total={len(results)} "
                f"(limit={limit})",
                file=sys.stderr,
            )

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

        # Dedupe; prefer CSR contacts that have email
        seen: set[str] = set()
        merged: list[dict[str, Any]] = []
        for p in sorted(results, key=_contact_relevance_key):
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
        raw = query.get("company_names") or query.get("companies") or query.get("company") or ""
        if isinstance(raw, list):
            name_candidates = [str(v).strip() for v in raw if str(v).strip()]
        else:
            joined = _join(raw)
            name_candidates = [joined] if joined else []
        keywords = _join(query.get("keywords") or "")
        nonprofit = _is_nonprofit_query(query)
        if not name_candidates and nonprofit:
            name_candidates = ["NGO"]
        if not name_candidates and not keywords and not query.get("company_domains"):
            return []

        geo = _geo_filters(query)
        want_cities = {
            c
            for c in _CITY_ZIPS
            if re.search(rf"\b{re.escape(c)}\b", _query_blob(query))
        }
        by_id: dict[str, dict] = {}

        def _ingest(rows: list) -> None:
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
            for r in rows:
                if not isinstance(r, dict):
                    continue
                cid = str(r.get("id") or r.get("companyId") or "").strip()
                key = cid or f"name:{(r.get('name') or r.get('companyName') or '')}"
                if key and key not in by_id:
                    by_id[key] = r

        def _post_company(body: dict[str, Any], label: str) -> None:
            try:
                resp = requests.post(
                    f"{self.BASE}/search/company",
                    headers=self._headers(),
                    json=body,
                    timeout=45,
                )
                if resp.status_code == 400:
                    body = dict(body)
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
                if resp.status_code >= 400:
                    print(
                        f"[zoominfo] company search {resp.status_code} for {label!r}: "
                        f"{(resp.text or '')[:200]}",
                        file=sys.stderr,
                    )
                    return
                _ingest((resp.json() or {}).get("data") or [])
            except Exception as e:
                print(f"[zoominfo] company search error ({label!r}): {e}", file=sys.stderr)

        # One companyName per request — comma-joining aliases confuses ZoomInfo.
        # Do not AND website on the same request; domain is a separate fallback.
        for names in (name_candidates or [""])[:6]:
            body: dict[str, Any] = {
                "rpp": min(max(int(limit), 1), 100),
                "page": 1,
            }
            if names and not re.search(
                r"\bngo\b|\bnonprofit\b|\bnon-profit\b", names, re.I
            ):
                body["companyName"] = names
            elif nonprofit or not names:
                body["companyName"] = names or "NGO"
            else:
                body["companyName"] = names
            blob = f"{names} {keywords}".lower()
            if re.search(r"foundation|trust|nonprofit|non-profit|ngo", blob):
                body["companyDescription"] = "NGO OR nonprofit OR foundation OR trust"
            if keywords and "companyName" not in body:
                body["companyKeywords"] = keywords
            body.update(geo)
            _post_company(body, names)
            if len(by_id) >= max(limit, 10):
                break

        if not by_id and query.get("company_domains"):
            for dom in (
                query["company_domains"]
                if isinstance(query["company_domains"], list)
                else [_join(query["company_domains"])]
            )[:3]:
                d = str(dom or "").strip()
                if not d:
                    continue
                body = {
                    "rpp": min(max(int(limit), 1), 100),
                    "page": 1,
                    "companyWebsite": d,
                }
                body.update(geo)
                _post_company(body, f"domain:{d}")
                if by_id:
                    break

        return list(by_id.values())

    def _contacts_for_companies(
        self,
        companies: list[dict[str, Any]],
        limit: int = 10,
        titles: Optional[list[str]] = None,
        expand: bool = True,
        domains: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        """Pull contacts for company IDs — title priority first, then broaden.

        Mirrors Phase 1 `zoominfo_search_contact`: try each title in order
        (e.g. Head CSR → CSR & Sustainability → …). Prefer people whose title
        actually looks like CSR/Sustainability (Anupam Das / Swati Bhattacharya
        style hits), then expand to other contacts at the firm / domain.
        """
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        title_list = [str(t).strip() for t in (titles or []) if str(t).strip()]
        domain_list = [str(d).strip().lower() for d in (domains or []) if str(d).strip()]

        def _append_rows(
            rows: list[dict[str, Any]],
            *,
            company_name: str = "",
            job_title: Optional[str] = None,
            prefer_csr: bool = False,
        ) -> int:
            nonlocal out
            if len(out) >= limit:
                return 0
            ranked = sorted(
                [r for r in rows if isinstance(r, dict)],
                key=lambda c: (
                    0 if _CSR_TITLE_RE.search(str(c.get("jobTitle") or "")) else 1,
                    0 if c.get("hasEmail") else 1,
                    0 if c.get("hasSupplementalEmail") else 1,
                ),
            )
            if prefer_csr:
                csr_only = [
                    r
                    for r in ranked
                    if _CSR_TITLE_RE.search(str(r.get("jobTitle") or ""))
                ]
                if csr_only:
                    with_email = [
                        r
                        for r in csr_only
                        if r.get("hasEmail") or r.get("hasSupplementalEmail")
                    ]
                    # Prefer CSR rows ZoomInfo flags as having email
                    ranked = with_email or csr_only
                # else keep full ranked (title filter may have been too strict)
            before = len(out)
            batch = self._enrich_contact_rows(ranked, limit=limit - len(out))
            for p in batch:
                key = (
                    p.get("email") or p.get("source_id") or p.get("name") or ""
                ).lower()
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                if not p.get("company") and company_name:
                    p["company"] = company_name
                if job_title and not p.get("matched_on"):
                    p["matched_on"] = f"{job_title} (ZI title priority)"
                out.append(p)
                if len(out) >= limit:
                    break
            return len(out) - before

        def _append_from_company(
            co: dict[str, Any], *, job_title: Optional[str] = None, prefer_csr: bool = False
        ) -> None:
            if len(out) >= limit:
                return
            cid = co.get("id") or co.get("companyId")
            if not cid:
                return
            body: dict[str, Any] = {
                "companyId": str(cid),
                "rpp": min(25, max(5, limit - len(out) + 5)),
                "page": 1,
            }
            if job_title:
                body["jobTitle"] = job_title
            try:
                resp = requests.post(
                    f"{self.BASE}/search/contact",
                    headers=self._headers(),
                    json=body,
                    timeout=45,
                )
                resp.raise_for_status()
                rows = (resp.json() or {}).get("data") or []
            except Exception as e:
                label = job_title or "(any)"
                print(
                    f"[zoominfo] contacts-for-company error ({label}): {e}",
                    file=sys.stderr,
                )
                return
            added = _append_rows(
                rows,
                company_name=str(co.get("name") or ""),
                job_title=job_title,
                prefer_csr=prefer_csr,
            )
            if added:
                print(
                    f"[zoominfo] title hit {job_title!r} → +{added} "
                    f"at {co.get('name') or co.get('id')}",
                    file=sys.stderr,
                )

        def _append_from_domain(
            domain: str, *, job_title: Optional[str] = None, prefer_csr: bool = False
        ) -> None:
            if len(out) >= limit or not domain:
                return
            body: dict[str, Any] = {
                "companyWebsite": domain,
                "rpp": min(25, max(5, limit - len(out) + 5)),
                "page": 1,
            }
            if job_title:
                body["jobTitle"] = job_title
            try:
                resp = requests.post(
                    f"{self.BASE}/search/contact",
                    headers=self._headers(),
                    json=body,
                    timeout=45,
                )
                resp.raise_for_status()
                rows = (resp.json() or {}).get("data") or []
            except Exception as e:
                print(
                    f"[zoominfo] domain contact error ({domain!r}/{job_title!r}): {e}",
                    file=sys.stderr,
                )
                return
            added = _append_rows(
                rows,
                company_name=domain,
                job_title=job_title or f"domain:{domain}",
                prefer_csr=prefer_csr,
            )
            if added:
                print(
                    f"[zoominfo] domain hit {domain!r} title={job_title!r} → +{added}",
                    file=sys.stderr,
                )

        def _csr_count() -> int:
            return sum(
                1
                for p in out
                if _CSR_TITLE_RE.search(str(p.get("title") or ""))
            )

        # Phase A — persona titles (require CSR-looking titles when possible)
        title_target = min(limit, max(3, (limit + 1) // 2))
        for title in title_list[:12]:
            if _csr_count() >= title_target:
                break
            for co in companies[:3]:
                if _csr_count() >= title_target:
                    break
                _append_from_company(co, job_title=title, prefer_csr=True)
            for dom in domain_list[:2]:
                if _csr_count() >= title_target:
                    break
                _append_from_domain(dom, job_title=title, prefer_csr=True)

        # Phase A2 — broad CSR/Sustainability keyword on domain (catches
        # "Head CSR & Sustainability" / "CMO & Head CSR" phrasing)
        if _csr_count() < title_target:
            for keyword in ("CSR", "Sustainability", "ESG"):
                if _csr_count() >= title_target:
                    break
                for dom in domain_list[:2]:
                    _append_from_domain(dom, job_title=keyword, prefer_csr=True)
                for co in companies[:2]:
                    _append_from_company(co, job_title=keyword, prefer_csr=True)

        # Phase B — broaden to other contacts at the same firms / domains
        if expand and len(out) < limit:
            print(
                f"[zoominfo] expanding beyond titles "
                f"({len(out)}/{limit} so far, csr={_csr_count()})",
                file=sys.stderr,
            )
            for co in companies:
                if len(out) >= limit:
                    break
                _append_from_company(co, job_title=None, prefer_csr=False)
            for dom in domain_list[:2]:
                if len(out) >= limit:
                    break
                _append_from_domain(dom, job_title=None, prefer_csr=False)

        out.sort(key=_contact_relevance_key)
        return out[:limit]

    def _enrich_contact_rows(
        self, contacts: list[dict[str, Any]], limit: int = 10
    ) -> list[dict[str, Any]]:
        """Search hits → enrich for email/mobile; never drop people if enrich is thin."""
        ranked = sorted(
            contacts,
            key=lambda c: (
                0 if c.get("hasEmail") else 1,
                0 if c.get("hasSupplementalEmail") else 1,
            ),
        )
        ranked = [c for c in ranked if isinstance(c, dict)][: max(limit, 10)]
        person_ids: list[Any] = []
        search_by_id: dict[str, dict[str, Any]] = {}
        for c in ranked:
            pid = c.get("personId") or c.get("id")
            if not pid:
                continue
            pid_s = str(pid)
            person_ids.append(pid)
            search_by_id[pid_s] = c
        person_ids = person_ids[:limit]
        if not person_ids:
            return [_row_to_prospect(c) for c in ranked[:limit]]

        enriched_by_id: dict[str, dict[str, Any]] = {}
        try:
            rows = self._enrich_by_ids(person_ids)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if row.get("errorMessage") or row.get("invalidInputFields"):
                    continue
                rid = str(row.get("id") or row.get("personId") or "")
                if not rid:
                    continue
                search_hit = search_by_id.get(rid) or {}
                if not _company_name(row) and search_hit:
                    row = {**row, "company": search_hit.get("company")}
                if not row.get("jobTitle") and search_hit.get("jobTitle"):
                    row = {**row, "jobTitle": search_hit.get("jobTitle")}
                enriched_by_id[rid] = _row_to_prospect(row)
        except Exception as e:
            print(f"[zoominfo] enrich-after-search error: {e}", file=sys.stderr)

        # If batch enrich returned almost nothing, try one-by-one for emails
        missing_email_ids = [
            pid
            for pid in person_ids
            if not (enriched_by_id.get(str(pid)) or {}).get("email")
        ]
        if missing_email_ids and len(enriched_by_id) < len(person_ids):
            for pid in missing_email_ids[: min(limit, 10)]:
                if str(pid) in enriched_by_id and (
                    enriched_by_id[str(pid)].get("email") or ""
                ).strip():
                    continue
                try:
                    one = self._enrich_by_ids([pid])
                except Exception:
                    continue
                for row in one:
                    if not isinstance(row, dict):
                        continue
                    if row.get("errorMessage") or row.get("invalidInputFields"):
                        continue
                    rid = str(row.get("id") or row.get("personId") or pid)
                    prospect = _row_to_prospect(row)
                    if not (prospect.get("email") or "").strip():
                        continue
                    search_hit = search_by_id.get(str(pid)) or {}
                    if not prospect.get("company") and search_hit:
                        prospect["company"] = _company_name(search_hit)
                    if not prospect.get("title") and search_hit.get("jobTitle"):
                        prospect["title"] = search_hit.get("jobTitle")
                    enriched_by_id[rid] = prospect
                    enriched_by_id[str(pid)] = prospect

        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for c in ranked[:limit]:
            pid = str(c.get("personId") or c.get("id") or "")
            prospect = enriched_by_id.get(pid) or _row_to_prospect(c)
            key = (
                (prospect.get("email") or "").strip().lower()
                or str(prospect.get("source_id") or pid)
                or (prospect.get("name") or "").strip().lower()
            )
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(prospect)
            if len(out) >= limit:
                break
        return out

    def enrich(
        self,
        identifier: dict[str, Any],
        *,
        linkedin_url: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Enrich one person. `linkedin_url=` maps to personLinkedInUrl / companyLinkedInUrl."""
        if not self._configured():
            return None

        linkedin = (
            linkedin_url
            or identifier.get("linkedin_url")
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
        kind = linkedin_url_kind(linkedin) if linkedin else ""
        if linkedin and kind == "in" and (not first or not last):
            parsed_first, parsed_last = names_from_linkedin_url(linkedin)
            first = first or parsed_first
            last = last or parsed_last
        if linkedin and kind == "company" and not company:
            company = company_name_from_linkedin_url(linkedin)

        # Primary: Enrich Contact by LinkedIn URL (try country + www hosts)
        if linkedin and kind == "in":
            hit = self._enrich_by_person_linkedin(linkedin)
            if hit:
                # Identity came from the URL — do not replace with a namesake who has email
                return hit
            last_partial = None
        else:
            last_partial = None

        # Fallback 1: Person Search by (first, last); keep only LinkedIn URL matches
        if linkedin and first and last:
            hit = self._search_person_then_enrich(
                first, last, company, linkedin_url=linkedin, require_linkedin_match=True
            )
            if hit and (hit.get("email") or "").strip():
                return hit
            if hit and not last_partial:
                last_partial = hit

        # Fallback 2: Company Enrich by domain/slug → Person Search within company
        if linkedin:
            co = self._enrich_company_by_linkedin(linkedin) if kind == "company" else None
            if not co and company:
                co = self._search_company_by_name(company)
            if co:
                co_name = str(
                    co.get("name") or co.get("companyName") or company or ""
                ).strip()
                domain = str(
                    co.get("website")
                    or co.get("companyWebsite")
                    or co.get("domain")
                    or ""
                ).strip()
                if first and last:
                    hit = self._search_person_then_enrich(
                        first,
                        last,
                        co_name,
                        linkedin_url=linkedin if kind == "in" else "",
                        domain=domain,
                        require_linkedin_match=bool(linkedin and kind == "in"),
                    )
                    if hit and (hit.get("email") or "").strip():
                        if not hit.get("industry"):
                            hit["industry"] = (
                                co.get("industry") or co.get("primaryIndustry") or ""
                            )
                        return hit
                    if hit and not last_partial:
                        last_partial = hit
                elif kind == "company":
                    # Company URL only — surface firmographics, no personal email
                    return {
                        **normalize(
                            {
                                "name": co_name,
                                "company": co_name,
                                "industry": co.get("industry")
                                or co.get("primaryIndustry")
                                or "",
                                "location": ", ".join(
                                    filter(
                                        None,
                                        [
                                            co.get("city"),
                                            co.get("state"),
                                            co.get("country"),
                                        ],
                                    )
                                ),
                                "about": co.get("description")
                                or co.get("companyDescription")
                                or "",
                                "linkedin_url": linkedin,
                            },
                            source=self.name,
                            source_id=str(co.get("id") or ""),
                        ),
                        "email": "",
                    }

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
            domain = (
                identifier.get("company_domain")
                or identifier.get("company_domains")
                or identifier.get("domain")
                or ""
            )
            if isinstance(domain, list):
                domain = domain[0] if domain else ""
            domain = str(domain or "").strip().lower().removeprefix("www.")
            if domain and "." in domain:
                match_input["companyWebsite"] = domain
            if linkedin and kind == "in":
                match_input["personLinkedInUrl"] = linkedin
            elif linkedin and kind == "company":
                match_input["companyLinkedInUrl"] = linkedin

        if (
            linkedin
            and kind == "in"
            and not (
                identifier.get("email")
                or identifier.get("source_id")
                or identifier.get("personId")
            )
        ):
            # URL + name already tried; first/last enrich would pick a namesake
            match_input = {}

        if match_input:
            hit = self._post_enrich_contact(match_input, linkedin=linkedin)
            if hit and (hit.get("email") or "").strip():
                return hit
            if hit and not last_partial:
                last_partial = hit

        return last_partial

    def _output_fields(self) -> list[str]:
        return list(self.OUTPUT_FIELDS)

    def _post_enrich_contact(
        self,
        match_input: dict[str, Any],
        *,
        linkedin: str = "",
    ) -> Optional[dict[str, Any]]:
        if not match_input:
            return None
        fields = list(self.OUTPUT_FIELDS)
        try:
            resp = requests.post(
                f"{self.BASE}/enrich/contact",
                headers=self._headers(),
                json={
                    "matchPersonInput": [match_input],
                    "outputFields": fields,
                },
                timeout=45,
            )
            if resp.status_code == 400:
                resp = requests.post(
                    f"{self.BASE}/enrich/contact",
                    headers=self._headers(),
                    json={
                        "matchPersonInput": [match_input],
                        "outputFields": list(self.OUTPUT_FIELDS_MIN),
                    },
                    timeout=45,
                )
            resp.raise_for_status()
            rows = _flatten_enrich_rows(resp.json())
            if not rows:
                return None
            if rows[0].get("errorMessage") or rows[0].get("invalidInputFields"):
                return None
            prospect = _row_to_prospect(rows[0])
            row_li = _linkedin_from_row(rows[0])
            if linkedin and row_li and not _linkedin_urls_match(linkedin, row_li):
                return None
            if linkedin:
                prospect["linkedin_url"] = extract_linkedin_url(linkedin) or linkedin
            return prospect
        except Exception as e:
            print(f"[zoominfo] enrich error: {e}", file=sys.stderr)
            return None

    def _enrich_by_person_linkedin(self, url: str) -> Optional[dict[str, Any]]:
        """Primary: ZoomInfo Enrich Contact by personLinkedInUrl (all host variants)."""
        last = None
        for variant in linkedin_url_variants(url):
            for key in ("personLinkedInUrl", "linkedInUrl"):
                hit = self._post_enrich_contact({key: variant}, linkedin=url)
                if not hit:
                    continue
                if (hit.get("email") or "").strip():
                    return hit
                last = last or hit
        return last

    def _search_person_then_enrich(
        self,
        first: str,
        last: str,
        company: str = "",
        *,
        linkedin_url: str = "",
        domain: str = "",
        require_linkedin_match: bool = False,
    ) -> Optional[dict[str, Any]]:
        try:
            search_body: dict[str, Any] = {
                "firstName": first,
                "lastName": last,
                "rpp": 10,
                "page": 1,
            }
            if company:
                search_body["companyName"] = company
            if domain and "." in domain:
                search_body["companyWebsite"] = (
                    domain.lower().removeprefix("www.").split("/")[0]
                )
            if linkedin_url:
                canon = extract_linkedin_url(linkedin_url) or linkedin_url
                search_body["linkedInUrl"] = canon
                search_body["personLinkedInUrl"] = canon
            resp = requests.post(
                f"{self.BASE}/search/contact",
                headers=self._headers(),
                json=search_body,
                timeout=45,
            )
            if resp.status_code == 400 and linkedin_url:
                search_body.pop("linkedInUrl", None)
                search_body.pop("personLinkedInUrl", None)
                resp = requests.post(
                    f"{self.BASE}/search/contact",
                    headers=self._headers(),
                    json=search_body,
                    timeout=45,
                )
            resp.raise_for_status()
            contacts = (resp.json() or {}).get("data") or []
            if not contacts:
                return None
            chosen = _pick_contact_for_linkedin(
                contacts, linkedin_url, require_match=require_linkedin_match
            )
            if chosen is None and linkedin_url and require_linkedin_match:
                ranked = [c for c in contacts if isinstance(c, dict)][:5]
                for c in ranked:
                    pid = c.get("personId") or c.get("id")
                    if not pid:
                        continue
                    rows = self._enrich_by_ids([pid])
                    if not rows:
                        continue
                    if not _linkedin_urls_match(
                        linkedin_url, _linkedin_from_row(rows[0])
                    ):
                        continue
                    prospect = _row_to_prospect(rows[0])
                    prospect["linkedin_url"] = (
                        extract_linkedin_url(linkedin_url) or linkedin_url
                    )
                    if not prospect.get("company"):
                        prospect["company"] = _company_name(c) or company
                    return prospect
                return None
            if chosen is None:
                return None
            pid = chosen.get("personId") or chosen.get("id")
            if not pid:
                prospect = _row_to_prospect(chosen)
                if linkedin_url:
                    prospect["linkedin_url"] = (
                        extract_linkedin_url(linkedin_url) or linkedin_url
                    )
                return prospect
            rows = self._enrich_by_ids([pid])
            if rows:
                prospect = _row_to_prospect(rows[0])
                if linkedin_url:
                    prospect["linkedin_url"] = (
                        extract_linkedin_url(linkedin_url) or linkedin_url
                    )
                if not prospect.get("company"):
                    prospect["company"] = _company_name(chosen) or company
                return prospect
            prospect = _row_to_prospect(chosen)
            if linkedin_url:
                prospect["linkedin_url"] = (
                    extract_linkedin_url(linkedin_url) or linkedin_url
                )
            return prospect
        except Exception as e:
            print(f"[zoominfo] linkedin→search enrich error: {e}", file=sys.stderr)
        return None

    def _enrich_company_by_linkedin(self, url: str) -> Optional[dict[str, Any]]:
        """Company Enrich by companyLinkedInUrl (or slug as companyName)."""
        try:
            resp = requests.post(
                f"{self.BASE}/enrich/company",
                headers=self._headers(),
                json={
                    "matchCompanyInput": [{"companyLinkedInUrl": url}],
                    "outputFields": [
                        "id",
                        "name",
                        "website",
                        "companyWebsite",
                        "industry",
                        "city",
                        "state",
                        "country",
                        "description",
                        "employeeCount",
                    ],
                },
                timeout=45,
            )
            if resp.status_code < 400:
                rows = _flatten_enrich_rows(resp.json())
                if rows and not (
                    rows[0].get("errorMessage") or rows[0].get("invalidInputFields")
                ):
                    return rows[0]
        except Exception as e:
            print(f"[zoominfo] company linkedin enrich: {e}", file=sys.stderr)
        slug_name = company_name_from_linkedin_url(url)
        if slug_name:
            return self._search_company_by_name(slug_name)
        return None

    def _search_company_by_name(self, name: str) -> Optional[dict[str, Any]]:
        hits = self._search_companies({"company_names": [name]}, limit=3)
        return hits[0] if hits else None

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
        if enr.status_code == 400:
            enr = requests.post(
                f"{self.BASE}/enrich/contact",
                headers=self._headers(),
                json={
                    "matchPersonInput": [{"personId": pid} for pid in person_ids],
                    "outputFields": list(self.OUTPUT_FIELDS_MIN),
                },
                timeout=60,
            )
        enr.raise_for_status()
        return _flatten_enrich_rows(enr.json())


def extract_linkedin_urls(text: str, *, limit: int = 100) -> list[str]:
    """Return unique LinkedIn /in/ and /company/ URLs found in text (paste-safe)."""
    seen: set[str] = set()
    out: list[str] = []
    blob = text or ""
    # Prefer the canonical https?://(www.)?linkedin.com/(in|company)/ regex first
    matches = list(_LINKEDIN_CANON_RE.finditer(blob)) + list(_LINKEDIN_RE.finditer(blob))
    for m in matches:
        raw = m.group(0)
        kind, slug = _linkedin_kind_slug(raw)
        if not slug:
            continue
        key = f"{kind}:{slug.lower()}"
        if key in seen:
            continue
        seen.add(key)
        out.append(f"https://www.linkedin.com/{kind}/{slug}")
        if len(out) >= max(1, int(limit)):
            break
    return out


def extract_linkedin_url(text: str) -> str:
    """Return the first LinkedIn profile or company URL found in text, or ''."""
    urls = extract_linkedin_urls(text or "", limit=1)
    return urls[0] if urls else ""


def linkedin_url_kind(url: str) -> str:
    """Return 'in' or 'company' (empty if not a LinkedIn URL)."""
    kind, _slug = _linkedin_kind_slug(url or "")
    return kind


def _linkedin_kind_slug(url: str) -> tuple[str, str]:
    m = _LINKEDIN_RE.search(url or "")
    if not m:
        return "", ""
    kind = (m.group(1) or "in").lower()
    slug = (m.group(2) or "").strip().strip("/")
    slug = slug.split("?")[0].split("#")[0].rstrip("/")
    try:
        from urllib.parse import unquote

        slug = unquote(slug)
    except Exception:
        pass
    return kind, slug


def names_from_linkedin_url(url: str) -> tuple[str, str]:
    """Best-effort first/last from /in/slug (e.g. damian-lawlor → Damian, Lawlor)."""
    kind, slug = _linkedin_kind_slug(url or "")
    if kind != "in" or not slug:
        return "", ""
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


def company_name_from_linkedin_url(url: str) -> str:
    """Best-effort company label from /company/slug."""
    kind, slug = _linkedin_kind_slug(url or "")
    if kind != "company" or not slug:
        return ""
    parts = [p for p in re.split(r"[-_]+", slug) if p and not p.isdigit()]
    return " ".join(p.capitalize() for p in parts)


def _linkedin_urls_match(a: str, b: str) -> bool:
    ka, sa = _linkedin_kind_slug(a)
    kb, sb = _linkedin_kind_slug(b)
    if not sa or not sb:
        return False
    return ka == kb and sa.lower().rstrip("/") == sb.lower().rstrip("/")


def linkedin_url_variants(url: str) -> list[str]:
    """Hosts ZoomInfo may store: original country host, www, and bare linkedin.com."""
    raw = (url or "").strip()
    kind, slug = _linkedin_kind_slug(raw)
    if not kind or not slug:
        return [raw] if raw else []
    hosts: list[str] = []
    host_m = re.search(r"https?://([^/]+)/", raw, re.I)
    orig_host = (host_m.group(1) if host_m else "").lower().rstrip(".")
    for host in (orig_host, "www.linkedin.com", "in.linkedin.com", "linkedin.com"):
        if host and host not in hosts:
            hosts.append(host)
    out: list[str] = []
    seen: set[str] = set()
    for host in hosts:
        candidate = f"https://{host}/{kind}/{slug}"
        key = candidate.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def _pick_contact_for_linkedin(
    contacts: list[Any],
    linkedin_url: str,
    *,
    require_match: bool = False,
) -> Optional[dict[str, Any]]:
    """Pick a search row. When a LinkedIn URL is required, never fall back to a namesake."""
    rows = [c for c in contacts if isinstance(c, dict)]
    if linkedin_url:
        for c in rows:
            if _linkedin_urls_match(linkedin_url, _linkedin_from_row(c)):
                return c
        if require_match:
            return None
    ranked = sorted(
        rows,
        key=lambda c: (
            0 if c.get("hasEmail") else 1,
            0 if c.get("hasSupplementalEmail") else 1,
        ),
    )
    return ranked[0] if ranked else None


def _first_linkedin(text: Any) -> str:
    return extract_linkedin_url(str(text or ""))


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


def _linkedin_from_row(row: dict[str, Any]) -> str:
    """ZoomInfo often returns LinkedIn under externalUrls, not linkedinUrl."""
    direct = (
        row.get("linkedinUrl")
        or row.get("linkedInUrl")
        or row.get("linkedin_url")
        or ""
    )
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    urls = row.get("externalUrls") or row.get("externalUrl") or []
    if isinstance(urls, dict):
        urls = [urls]
    if isinstance(urls, list):
        for item in urls:
            if not isinstance(item, dict):
                continue
            typ = str(item.get("type") or "").lower()
            url = str(item.get("url") or "").strip()
            if "linkedin" in typ or "linkedin.com" in url.lower():
                return url
    return ""


def _phone_from_row(row: dict[str, Any]) -> tuple[str, str]:
    """Return (phone, mobile) preferring non-empty values."""
    phone = str(row.get("phone") or row.get("directPhone") or "").strip()
    mobile = str(row.get("mobilePhone") or row.get("mobile") or "").strip()
    return phone, mobile


def _row_to_prospect(row: dict[str, Any]) -> dict[str, Any]:
    phone, mobile = _phone_from_row(row)
    linkedin = _linkedin_from_row(row)
    street = str(row.get("street") or "").strip()
    location = ", ".join(
        filter(
            None,
            [
                street,
                row.get("city"),
                row.get("state"),
                row.get("zipCode"),
                row.get("country"),
            ],
        )
    )
    raw = {
        "id": row.get("id") or row.get("personId"),
        "first_name": row.get("firstName"),
        "last_name": row.get("lastName"),
        "email": row.get("email"),
        "phone": phone or mobile,
        "mobile": mobile,
        "title": row.get("jobTitle"),
        "company": _company_name(row),
        "linkedin_url": linkedin,
        "location": location,
        "seniority": row.get("managementLevel"),
        "department": row.get("department"),
        "industry": row.get("companyIndustry")
        or row.get("industry")
        or row.get("industryCodes")
        or "",
        "about": row.get("bio")
        or row.get("personOverview")
        or row.get("description")
        or "",
        "external_urls": row.get("externalUrls") or [],
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


# Same persona ladder as Bulk Enrich / Phase 1 — tuned to ZoomInfo title phrasing
# (e.g. "Head CSR & Sustainability", "CMO & Head CSR"), not only "CSR Head".
CSR_TITLE_PRIORITY: list[str] = [
    "Head CSR",
    "Head of CSR",
    "CSR Head",
    "CSR & Sustainability",
    "Head CSR & Sustainability",
    "CSR and Sustainability",
    "Corporate Social Responsibility",
    "Head of Sustainability",
    "Sustainability Head",
    "Head of ESG",
    "ESG Head",
    "Chief Marketing Officer",
    "Head of Partnerships",
    "Director of Partnerships",
    "Head of Corporate Partnerships",
    "CSR Manager",
    "Sustainability",
    "CSR",
]

_CSR_TITLE_RE = re.compile(
    r"\bcsr\b|sustainab|esg\b|corporate\s+social|social\s+impact|"
    r"partnerships?\b|community\s+(relations|development)|foundation\b",
    re.I,
)

_STL_EMAIL_RE = re.compile(r"@(?:stl\.tech|sterlitetech\.com)\b", re.I)


def _contact_relevance_key(row: dict[str, Any]) -> tuple:
    """Sort key: CSR-with-email first, then CSR, then stl.tech, then any email."""
    title = str(row.get("title") or row.get("jobTitle") or "")
    email = str(row.get("email") or "").strip().lower()
    is_csr = bool(_CSR_TITLE_RE.search(title))
    # 0 = CSR + email, 1 = CSR no email, 2 = non-CSR
    csr_tier = 0 if (is_csr and email) else (1 if is_csr else 2)
    csr_strong = 0 if re.search(r"\bcsr\b", title, re.I) else 1
    stl = 0 if email and _STL_EMAIL_RE.search(email) else 1
    has_email = 0 if email else 1
    name = str(row.get("name") or "").lower()
    return (csr_tier, csr_strong, stl, has_email, name)

NGO_TITLE_PRIORITY: list[str] = [
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


def _titles_from_query(query: dict[str, Any]) -> list[str]:
    raw = query.get("titles") or query.get("title") or []
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
        return parts
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    return []


def _default_csr_titles() -> list[str]:
    """Merge Drive CSR persona titles with the hardcoded ZoomInfo-friendly ladder."""
    base = list(CSR_TITLE_PRIORITY)
    drive_titles: list[str] = []
    try:
        from core import drive_db

        for p in drive_db.load_persona_targets() or []:
            if not isinstance(p, dict):
                continue
            pid = str(p.get("id") or "").lower()
            label = str(p.get("label") or "").lower()
            titles = [str(t).strip() for t in (p.get("titles") or []) if str(t).strip()]
            if titles and ("csr" in pid or "csr" in label):
                drive_titles = titles
                break
    except Exception:
        pass
    if not drive_titles:
        return base
    seen: set[str] = set()
    out: list[str] = []
    for t in drive_titles + base:
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out


def _title_cascade_for_query(query: dict[str, Any]) -> tuple[list[str], bool]:
    """Return (titles_in_priority_order, expand_to_other_contacts).

    - Explicit titles in the query → try those first; expand only if title_expand.
    - Corporate company search with no titles → CSR Head ladder, then expand.
    - Nonprofit search with no titles → NGO leadership ladder, then expand.
    """
    explicit = _titles_from_query(query)
    if explicit:
        # Still broaden when title hits are thin (same as CSR ladder behavior).
        expand = bool(query.get("title_expand", True))
        return explicit, expand
    if _is_nonprofit_query(query):
        return list(NGO_TITLE_PRIORITY), True
    # Named company contact search (Sterlite Tech, etc.)
    has_company = bool(
        query.get("company_names")
        or query.get("companies")
        or query.get("company")
        or query.get("company_domains")
        or query.get("company_id")
        or query.get("companyId")
    )
    if has_company:
        return _default_csr_titles(), True
    return [], True


# Short user phrases → ZoomInfo-friendly legal / brand names
_COMPANY_ALIASES: dict[str, list[str]] = {
    "sterlite tech": ["Sterlite Technologies", "Sterlite Technologies Limited", "STL"],
    "sterlite technology": ["Sterlite Technologies", "Sterlite Technologies Limited"],
    "sterlite technologies": ["Sterlite Technologies", "Sterlite Technologies Limited"],
    "sterlite": ["Sterlite Technologies", "Sterlite Technologies Limited"],
}

# Optional website domains to help company match
_COMPANY_DOMAINS: dict[str, list[str]] = {
    "sterlite tech": ["sterlitetech.com", "stl.tech"],
    "sterlite technology": ["sterlitetech.com", "stl.tech"],
    "sterlite technologies": ["sterlitetech.com", "stl.tech"],
    "sterlite": ["sterlitetech.com", "stl.tech"],
}


def _with_company_name_aliases(query: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow-copied query with expanded company_names for better ZI hits."""
    names = query.get("company_names") or query.get("companies") or query.get("company")
    if isinstance(names, str):
        names_list = [names]
    elif isinstance(names, list):
        names_list = [str(x) for x in names if x]
    else:
        return query
    if not names_list:
        return query
    expanded: list[str] = []
    seen: set[str] = set()
    domain_extra: list[str] = []
    for raw in names_list:
        key = re.sub(r"\s+", " ", str(raw or "").strip().lower())
        for cand in [raw] + _COMPANY_ALIASES.get(key, []):
            c = str(cand).strip()
            if not c:
                continue
            lk = c.lower()
            if lk in seen:
                continue
            seen.add(lk)
            expanded.append(c)
        for d in _COMPANY_DOMAINS.get(key, []):
            if d and d not in domain_extra:
                domain_extra.append(d)
    out = dict(query)
    if expanded != names_list:
        out["company_names"] = expanded
    if domain_extra:
        existing = out.get("company_domains") or out.get("domains") or []
        if isinstance(existing, str):
            existing = [existing] if existing.strip() else []
        elif not isinstance(existing, list):
            existing = []
        merged_doms = list(existing)
        for d in domain_extra:
            if d not in merged_doms:
                merged_doms.append(d)
        out["company_domains"] = merged_doms
    return out


def _rank_companies_for_query(
    companies: list[dict[str, Any]], needle: str
) -> list[dict[str, Any]]:
    """Prefer firms whose name matches the user query (Sterlite Technologies > noise)."""
    needle_n = re.sub(r"[^a-z0-9]+", " ", (needle or "").lower()).strip()
    tokens = [t for t in needle_n.split() if len(t) >= 4]

    def score(co: dict[str, Any]) -> tuple[int, str]:
        name = str(co.get("name") or co.get("companyName") or "").lower()
        name_n = re.sub(r"[^a-z0-9]+", " ", name).strip()
        website = str(
            co.get("website")
            or co.get("companyWebsite")
            or co.get("domain")
            or ""
        ).lower()
        sc = 0
        if needle_n and needle_n in name_n:
            sc += 100
        if name_n and needle_n and name_n in needle_n:
            sc += 40
        for t in tokens:
            if t in name_n:
                sc += 20
        # Prefer exact-ish brand hits over vague subsidiaries
        if "sterlite" in tokens and "sterlite" in name_n and "technolog" in name_n:
            sc += 50
        if "stl.tech" in website or "sterlitetech.com" in website:
            sc += 80
        if re.search(r"\bstl\b", name_n) and "sterlite" in name_n:
            sc += 30
        return (-sc, name)

    ranked = sorted([c for c in companies if isinstance(c, dict)], key=score)
    # Drop zero-signal firms when we have a distinctive token
    if tokens:
        kept = [
            c
            for c in ranked
            if any(
                t in re.sub(r"[^a-z0-9]+", " ", str(c.get("name") or "").lower())
                for t in tokens
            )
            or "stl.tech"
            in str(c.get("website") or c.get("companyWebsite") or "").lower()
            or "sterlitetech.com"
            in str(c.get("website") or c.get("companyWebsite") or "").lower()
        ]
        if kept:
            return kept
    return ranked


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

    raw_names = query.get("company_names") or ""
    if isinstance(raw_names, list):
        # Prefer the longest alias (e.g. "Sterlite Technologies Limited") over
        # short nicknames when doing direct /search/contact.
        cands = [str(x).strip() for x in raw_names if str(x).strip()]
        names = max(cands, key=len) if cands else ""
    else:
        names = _join(raw_names)
    if names:
        body["companyName"] = names
    elif _is_nonprofit_query(query):
        body["companyName"] = "NGO"
        body["companyDescription"] = "NGO OR nonprofit OR foundation OR trust"

    if query.get("company_domains"):
        body["companyWebsite"] = _join(query["company_domains"])

    company_id = query.get("company_id") or query.get("companyId")
    if company_id:
        body["companyId"] = str(company_id)

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
