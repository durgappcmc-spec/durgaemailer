# NOTE: Durable prospect list — reuse saved contacts before calling ZoomInfo again.
from __future__ import annotations

import re
import sys
import threading
from datetime import datetime, timezone
from typing import Any, Optional

_LOCK = threading.Lock()
_CACHE: Optional[list[dict[str, Any]]] = None
_LOADED = False

_ORG_NOISE = re.compile(
    r"\b(pvt\.?|private|ltd\.?|limited|inc\.?|llc|foundation|trust|society|"
    r"ngo|org(?:anisation|anization)?|the)\b",
    re.I,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = _ORG_NOISE.sub(" ", s)
    s = re.sub(r"[^a-z0-9@.+_\- ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _prospect_key(p: dict[str, Any]) -> str:
    email = _norm(str(p.get("email") or ""))
    if email and "@" in email:
        return f"email:{email}"
    sid = _norm(str(p.get("source_id") or ""))
    if sid:
        return f"sid:{sid}"
    name = _norm(str(p.get("name") or ""))
    company = _norm(str(p.get("company") or p.get("organization") or p.get("org") or ""))
    return f"nc:{name}|{company}"


def _load() -> list[dict[str, Any]]:
    global _CACHE, _LOADED
    if _LOADED and _CACHE is not None:
        return _CACHE
    rows: list[dict[str, Any]] = []
    try:
        from core.durable_store import load_json_blob

        # Always allow Drive on cold start (Render wipes local disk each deploy)
        data = load_json_blob("prospect_list", allow_sheets=True)
        if isinstance(data, list):
            rows = [r for r in data if isinstance(r, dict)]
    except Exception as e:
        print(f"[prospect_list] load failed: {e}", file=sys.stderr)
    _CACHE = rows
    _LOADED = True
    return _CACHE


def _persist(rows: list[dict[str, Any]]) -> bool:
    """Write prospect list to Drive (+ Sheets). Returns True if Drive sync ok."""
    global _CACHE, _LOADED
    _CACHE = rows
    _LOADED = True
    try:
        from core.durable_store import save_json_blob

        ok = bool(save_json_blob("prospect_list", rows[-1000:]))
        print(
            f"[prospect_list] persist n={len(rows)} drive_ok={ok}",
            file=sys.stderr,
        )
        if not ok:
            print(
                "[prospect_list] WARNING: Drive/Sheets sync reported failure "
                "— contacts may be local-only until next successful save",
                file=sys.stderr,
            )
        return ok
    except Exception as e:
        print(f"[prospect_list] save failed: {e}", file=sys.stderr)
        return False


def _is_mailbox_noise(p: dict[str, Any]) -> bool:
    """Gmail-derived rows must not live in the ZoomInfo Saved prospect list."""
    src = str(p.get("source") or "").strip().lower()
    return src in ("gmail", "gmail_contacts", "gmail_extract")


def _scrub_mailbox_noise(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if isinstance(r, dict) and not _is_mailbox_noise(r)]


def reload_from_drive() -> int:
    """Invalidate cache and pull contacts from Google Drive. Returns count."""
    global _CACHE, _LOADED
    with _LOCK:
        # Keep any ZoomInfo rows saved this session — a stale Drive blob must
        # not replace them on Prospects "Refresh" / page open.
        prev = list(_CACHE) if _LOADED and isinstance(_CACHE, list) else []
        _LOADED = False
        _CACHE = None
        rows = list(_load())
        if prev:
            try:
                from core.durable_store import _merge_prospect_lists

                rows = _merge_prospect_lists(rows, prev)
            except Exception:
                # Fallback: prefer previous session rows when merge helper missing
                by_key = {_prospect_key(r): r for r in rows if isinstance(r, dict)}
                for r in prev:
                    if not isinstance(r, dict):
                        continue
                    by_key[_prospect_key(r)] = {**by_key.get(_prospect_key(r), {}), **r}
                rows = list(by_key.values())
        cleaned = _scrub_mailbox_noise(rows)
        if len(cleaned) != len(rows) or (prev and cleaned):
            removed = max(0, len(rows) - len(cleaned))
            if removed:
                print(
                    f"[prospect_list] scrubbed {removed} "
                    f"gmail mailbox rows from Saved list",
                    file=sys.stderr,
                )
            _CACHE = cleaned
            _LOADED = True
            try:
                from core.drive_store import is_drive_degraded

                degraded = is_drive_degraded()
            except Exception:
                degraded = False
            if degraded:
                try:
                    from core.durable_store import save_json_blob

                    save_json_blob("prospect_list", cleaned[-1000:], sync_sheets=True)
                except Exception as e:
                    print(f"[prospect_list] sheets-only scrub save failed: {e}", file=sys.stderr)
            else:
                _persist(cleaned)
            rows = cleaned
        else:
            _CACHE = cleaned
            _LOADED = True
            rows = cleaned
        return len(rows)


def save_prospects(prospects: list[dict[str, Any]]) -> int:
    """Merge prospects into the durable list. Returns count newly saved/updated.

    Always attempts Drive sync when anything changed. Callers should treat a
    positive return as 'merged into list'; verify Drive via last persist logs
    or save_prospects_to_drive().
    """
    if not prospects:
        return 0
    from connectors import sanitize_prospect

    with _LOCK:
        global _CACHE, _LOADED
        rows = _scrub_mailbox_noise(list(_load()))
        # Safety: if in-memory list is empty, re-pull Drive before merge/upload
        if not rows:
            _LOADED = False
            _CACHE = None
            rows = _scrub_mailbox_noise(list(_load()))
        by_key = {_prospect_key(r): i for i, r in enumerate(rows)}
        changed = 0
        for p in prospects:
            if not p or p.get("error") or p.get("research_only"):
                continue
            p = sanitize_prospect(p)
            if _is_mailbox_noise(p):
                continue
            # Need at least a name or email or company signal
            if not (
                (p.get("email") or "").strip()
                or (p.get("name") or "").strip()
                or (p.get("company") or "").strip()
            ):
                continue
            # Tag provider so Saved list can show ZoomInfo vs Gmail
            if not (p.get("source") or "").strip():
                p["source"] = "zoominfo"
            key = _prospect_key(p)
            row = {
                **{k: v for k, v in p.items() if v not in (None, "", [], {})},
                "saved_at": _now(),
            }
            if key in by_key:
                idx = by_key[key]
                old = sanitize_prospect(rows[idx])
                merged = {**old, **row}
                for field in ("email", "phone", "mobile", "linkedin_url", "title", "name"):
                    if not (merged.get(field) or "").strip() and (old.get(field) or "").strip():
                        merged[field] = old[field]
                # Prefer zoominfo over gmail when merging same email
                if (old.get("source") or "") == "gmail" and (
                    str(row.get("source") or "").startswith("zoom")
                ):
                    merged["source"] = row.get("source") or "zoominfo"
                rows[idx] = sanitize_prospect(merged)
            else:
                by_key[key] = len(rows)
                rows.append(row)
            changed += 1
        if changed:
            drive_ok = _persist(rows)
            if not drive_ok:
                # One retry after clearing Drive file-id cache
                try:
                    from core import drive_store

                    drive_store.clear_file_cache()
                except Exception:
                    pass
                drive_ok = _persist(rows)
                print(
                    f"[prospect_list] retry persist drive_ok={drive_ok}",
                    file=sys.stderr,
                )
        return changed


def save_prospects_to_drive(prospects: list[dict[str, Any]]) -> dict[str, Any]:
    """Explicit ZoomInfo/search save with status for Chat UI."""
    n = save_prospects(prospects)
    total = 0
    try:
        total = len(all_prospects())
    except Exception:
        total = n
    return {"upserted": n, "total": total}


def repair_saved_prospects() -> int:
    """Fix name/email mix-ups already on the saved list. Returns rows changed."""
    from connectors import sanitize_prospect

    with _LOCK:
        rows = list(_load())
        fixed = 0
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for p in rows:
            clean = sanitize_prospect(p)
            if clean.get("name") != p.get("name") or clean.get("email") != p.get("email"):
                fixed += 1
            key = _prospect_key(clean)
            if key in seen:
                continue
            seen.add(key)
            out.append(clean)
        if fixed or len(out) != len(rows):
            _persist(out)
        return fixed


def all_prospects() -> list[dict[str, Any]]:
    from connectors import sanitize_prospect

    with _LOCK:
        return [sanitize_prospect(p) for p in _load()]


def _company_tokens(s: str) -> list[str]:
    stop = {
        "ltd",
        "llc",
        "inc",
        "pvt",
        "private",
        "limited",
        "the",
        "and",
        "of",
        "co",
        "corp",
        "company",
    }
    return [t for t in _norm(s).split() if len(t) > 2 and t not in stop]


def _companies_match(needle: str, company_blob: str, website: str = "") -> bool:
    """Strict-ish org match — avoid 'tech' matching every '* tech' query."""
    needle = _norm(needle)
    company_blob = _norm(company_blob)
    website = _norm(website)
    if not needle or len(needle) < 2:
        return False
    if company_blob and (needle in company_blob or company_blob == needle):
        return True
    if website and (
        needle in website
        or (len(needle) >= 4 and needle.replace(" ", "") in website.replace(" ", ""))
    ):
        return True
    # Allow blob⊂needle only when the saved company name is substantial
    if company_blob and len(company_blob) >= 6 and company_blob in needle:
        return True
    n_toks = _company_tokens(needle)
    if not n_toks or not company_blob:
        return False
    distinctive = [t for t in n_toks if len(t) >= 5]
    if distinctive and not any(t in company_blob for t in distinctive):
        return False
    hits = sum(1 for t in n_toks if t in company_blob)
    need = max(1, (len(n_toks) + 1) // 2)
    return hits >= need


def find_by_company(
    company: str,
    *,
    limit: int = 50,
    require_email: bool = False,
) -> list[dict[str, Any]]:
    """Return saved contacts whose company matches the org name/domain."""
    needle = _norm(company)
    if not needle or len(needle) < 2:
        return []
    hits: list[dict[str, Any]] = []
    with _LOCK:
        for p in _load():
            company_blob = _norm(
                " ".join(
                    str(p.get(k) or "")
                    for k in ("company", "organization", "org", "org_website")
                )
            )
            website = _norm(str(p.get("org_website") or p.get("website") or ""))
            if _companies_match(needle, company_blob, website):
                hits.append(p)
            if len(hits) >= max(limit * 3, limit):
                break
    if require_email:
        hits = [p for p in hits if (p.get("email") or "").strip()]
    # Prefer emails, then mobiles
    hits.sort(
        key=lambda p: (
            0 if (p.get("email") or "").strip() else 1,
            0 if (p.get("mobile") or p.get("phone") or "").strip() else 1,
            str(p.get("name") or ""),
        )
    )
    return hits[:limit]


def find_by_person(
    name: str,
    *,
    company: str = "",
    limit: int = 10,
) -> list[dict[str, Any]]:
    needle = _norm(name)
    if not needle or len(needle) < 2:
        return []
    company_n = _norm(company)
    hits: list[dict[str, Any]] = []
    with _LOCK:
        for p in _load():
            pname = _norm(str(p.get("name") or ""))
            first = _norm(str(p.get("first_name") or ""))
            last = _norm(str(p.get("last_name") or ""))
            email = _norm(str(p.get("email") or ""))
            if not (
                needle in pname
                or pname in needle
                or needle == first
                or (first and last and needle in f"{first} {last}")
                or (needle in email and "@" not in needle)
            ):
                continue
            if company_n:
                pc = _norm(str(p.get("company") or ""))
                if company_n not in pc and pc not in company_n:
                    continue
            hits.append(p)
            if len(hits) >= limit:
                break
    return hits[:limit]


def lookup_for_query(
    query: dict[str, Any],
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Match a PROSPECT_SEARCH-style query against the saved list."""
    companies = []
    for key in ("company_names", "companies", "company"):
        val = query.get(key)
        if isinstance(val, list):
            companies.extend(str(x) for x in val if x)
        elif isinstance(val, str) and val.strip():
            companies.append(val.strip())
    domains = []
    for key in ("company_domains", "domains"):
        val = query.get(key)
        if isinstance(val, list):
            domains.extend(str(x) for x in val if x)
        elif isinstance(val, str) and val.strip():
            domains.append(val.strip())

    titles = []
    for key in ("titles", "title"):
        val = query.get(key)
        if isinstance(val, list):
            titles.extend(_norm(str(x)) for x in val if x)
        elif isinstance(val, str) and val.strip():
            titles.append(_norm(val))

    keywords = []
    for key in ("keywords", "keyword", "name"):
        val = query.get(key)
        if isinstance(val, list):
            keywords.extend(_norm(str(x)) for x in val if x)
        elif isinstance(val, str) and val.strip():
            keywords.append(_norm(val))

    hits: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(rows: list[dict[str, Any]]) -> None:
        for p in rows:
            k = _prospect_key(p)
            if k in seen:
                continue
            seen.add(k)
            hits.append(p)

    for c in companies:
        _add(find_by_company(c, limit=limit))
    for d in domains:
        _add(find_by_company(d, limit=limit))

    # If only keywords/name, search person + company text
    if not hits and (keywords or titles):
        with _LOCK:
            for p in _load():
                blob = _norm(
                    " ".join(
                        str(p.get(k) or "")
                        for k in ("name", "title", "company", "email", "department")
                    )
                )
                ok = True
                if keywords and not any(k and k in blob for k in keywords):
                    ok = False
                if titles and not any(t and t in blob for t in titles):
                    # titles are soft if keywords matched company
                    if not keywords:
                        ok = False
                if ok:
                    _add([p])
                if len(hits) >= limit:
                    break

    if titles and hits:
        titled = [
            p
            for p in hits
            if any(t and t in _norm(str(p.get("title") or "")) for t in titles)
        ]
        if titled:
            hits = titled + [p for p in hits if p not in titled]

    return hits[:limit]


def search_saved(
    *,
    name: str = "",
    organisation: str = "",
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Filter saved contacts by name and/or organisation (substring, case-insensitive)."""
    from connectors import sanitize_prospect

    name_n = _norm(name)
    org_n = _norm(organisation)
    hits: list[dict[str, Any]] = []
    with _LOCK:
        for p in _load():
            p = sanitize_prospect(p)
            if name_n:
                blob = _norm(
                    " ".join(
                        str(p.get(k) or "")
                        for k in ("name", "first_name", "last_name", "email", "title")
                    )
                )
                if name_n not in blob:
                    continue
            if org_n:
                org_blob = _norm(
                    " ".join(
                        str(p.get(k) or "")
                        for k in ("company", "organization", "org", "org_website", "website")
                    )
                )
                if org_n not in org_blob and org_blob not in org_n:
                    continue
            hits.append(p)
            if len(hits) >= limit:
                break
    hits.sort(
        key=lambda p: (
            _norm(str(p.get("company") or "")),
            _norm(str(p.get("name") or "")),
        )
    )
    return hits


def has_email(row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    return bool((row.get("email") or "").strip())


def count_with_email(rows: list[dict[str, Any]] | None) -> int:
    return sum(1 for p in (rows or []) if has_email(p))


def saved_contacts_are_usable(
    rows: list[dict[str, Any]] | None,
    *,
    min_with_email: int = 1,
) -> bool:
    """True when saved contacts already have enough emails (skip ZoomInfo).

    Missing email is not “enough” — callers should auto ZoomInfo instead
    of asking the user to say refresh.
    """
    return count_with_email(rows) >= max(1, int(min_with_email or 1))


def enough_emailed_contacts(
    rows: list[dict[str, Any]] | None,
    *,
    limit: int = 10,
    specific: bool = False,
) -> bool:
    """Whether the saved list is complete enough to skip a ZoomInfo search.

    Requires enough *emailed* contacts for the requested limit — a single
    saved email must not stop ZoomInfo from looking up more people/details.
    """
    rows = rows or []
    with_email = count_with_email(rows)
    if with_email <= 0:
        return False
    want = max(1, int(limit or 1))
    # Company-specific: need most of the requested volume (cap at 5) with email
    if specific:
        needed = min(want, 5)
    else:
        needed = min(want, 3)
    # Also require no large hole of name-only stubs on the matched set
    if len(rows) >= needed and (len(rows) - with_email) > max(1, needed // 2):
        return False
    return with_email >= needed


def wants_force_refresh(user_msg: str) -> bool:
    return bool(
        re.search(
            r"\b(refresh|re-?search|zoom\s*info\s+again|search\s+again|"
            r"look\s+up\s+again|force\s+zoom|ignore\s+(saved|cache|memory|list))\b",
            user_msg or "",
            re.I,
        )
    )
