# NOTE: Drive-backed document store for chats, drafts, bulk jobs, traces, Gemini logs.
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from cachetools import LRUCache
from tenacity import retry, stop_after_attempt, wait_exponential

from core import drive_store

_ROOT_PREFIX = "DurgaEmailer"
_CACHE_DIR = Path("./.cache/drive_db")
_LOCK = threading.RLock()
_INDEX_ETAGS: dict[str, str] = {}
_MEM_CACHE: LRUCache = LRUCache(maxsize=256)
_MEM_CACHE_AT: dict[str, float] = {}
_MEM_TTL = 30.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _ym(ts: Optional[str] = None) -> str:
    if ts:
        try:
            return ts[:7]
        except Exception:
            pass
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _path(*parts: str) -> str:
    return "/".join([_ROOT_PREFIX, *[p.strip("/").replace("\\", "/") for p in parts if p]])


def _cache_file(name: str) -> Path:
    safe = name.replace("/", "__").replace("\\", "__")
    return _CACHE_DIR / f"{safe}.json"


def _write_local_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_local(path: Path) -> Optional[Any]:
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, min=0.5, max=8))
def _upload(name: str, payload: Any) -> bool:
    ok = drive_store.upload_json(name, payload)
    if not ok:
        raise RuntimeError(f"drive upload failed: {name}")
    return True


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, min=0.5, max=8))
def _download(name: str) -> Any:
    return drive_store.download_json(name)


def _get(name: str, *, default: Any = None, use_cache: bool = True) -> Any:
    with _LOCK:
        now = time.time()
        if use_cache and name in _MEM_CACHE:
            if now - _MEM_CACHE_AT.get(name, 0) < _MEM_TTL:
                return _MEM_CACHE[name]
        local = _read_local(_cache_file(name))
        if local is not None and use_cache:
            _MEM_CACHE[name] = local
            _MEM_CACHE_AT[name] = now
            # refresh from Drive in background-ish: try once
        try:
            remote = _download(name)
        except Exception as e:
            print(f"[drive_db] download {name}: {e}", file=sys.stderr)
            remote = None
        if remote is None:
            if local is not None:
                return local
            return default
        _MEM_CACHE[name] = remote
        _MEM_CACHE_AT[name] = now
        try:
            _write_local_atomic(_cache_file(name), remote)
        except Exception:
            pass
        return remote


def _put(name: str, payload: Any) -> None:
    with _LOCK:
        _MEM_CACHE[name] = payload
        _MEM_CACHE_AT[name] = time.time()
        try:
            _write_local_atomic(_cache_file(name), payload)
        except Exception as e:
            print(f"[drive_db] local cache write {name}: {e}", file=sys.stderr)
        try:
            _upload(name, payload)
        except Exception as e:
            print(f"[drive_db] upload {name}: {e}", file=sys.stderr)
            raise


def _append_jsonl(name: str, record: dict) -> None:
    """Load-append-save for monthly jsonl-as-json-array compatibility on Drive."""
    with _LOCK:
        existing = _get(name, default=[], use_cache=False) or []
        if not isinstance(existing, list):
            existing = []
        existing.append(record)
        # keep last 50k events per month file
        if len(existing) > 50000:
            existing = existing[-50000:]
        _put(name, existing)


def invalidate_cache(name: Optional[str] = None) -> None:
    with _LOCK:
        if name is None:
            _MEM_CACHE.clear()
            _MEM_CACHE_AT.clear()
        else:
            _MEM_CACHE.pop(name, None)
            _MEM_CACHE_AT.pop(name, None)


# ── Bulk jobs ──────────────────────────────────────────────────────────────


def save_bulk_job(job_id: str, job: dict) -> None:
    job = dict(job)
    job["job_id"] = job_id
    job["updated_at"] = _now()
    _put(_path("bulk_jobs", f"{job_id}.json"), job)
    idx = _get(_path("bulk_jobs_index.json"), default=[]) or []
    if not isinstance(idx, list):
        idx = []
    entry = {
        "job_id": job_id,
        "created_at": job.get("created_at"),
        "updated_at": job["updated_at"],
        "current_phase": job.get("current_phase"),
        "totals": job.get("totals"),
        "row_count": len(job.get("rows") or []),
    }
    idx = [e for e in idx if e.get("job_id") != job_id]
    idx.insert(0, entry)
    _put(_path("bulk_jobs_index.json"), idx[:200])


def load_bulk_job(job_id: str) -> dict:
    data = _get(_path("bulk_jobs", f"{job_id}.json"), default=None)
    if not data:
        raise KeyError(f"bulk job not found: {job_id}")
    return data


def list_bulk_jobs(limit: int = 50) -> list[dict]:
    idx = _get(_path("bulk_jobs_index.json"), default=[]) or []
    if not isinstance(idx, list):
        return []
    return idx[:limit]


def update_bulk_row(job_id: str, row_id: str, patch: dict) -> None:
    with _LOCK:
        job = load_bulk_job(job_id)
        rows = job.get("rows") or []
        found = False
        for i, row in enumerate(rows):
            if row.get("row_id") == row_id:
                merged = dict(row)
                merged.update(patch)
                rows[i] = merged
                found = True
                break
        if not found:
            raise KeyError(f"row {row_id} not in job {job_id}")
        job["rows"] = rows
        save_bulk_job(job_id, job)


def advance_job_phase(
    job_id: str,
    new_phase: str,
    approved_row_ids: list[str],
    phase2_config: dict,
) -> None:
    with _LOCK:
        job = load_bulk_job(job_id)
        approved = set(approved_row_ids or [])
        for row in job.get("rows") or []:
            if row.get("row_id") in approved:
                row["approved_for_phase2"] = True
            else:
                row["approved_for_phase2"] = bool(row.get("approved_for_phase2"))
        job["current_phase"] = new_phase
        job["phase2_config"] = phase2_config or {}
        totals = job.setdefault("totals", {})
        p2 = totals.setdefault("phase2", {})
        p2["approved"] = sum(
            1 for r in (job.get("rows") or []) if r.get("approved_for_phase2")
        )
        save_bulk_job(job_id, job)


# ── Persona targets ────────────────────────────────────────────────────────


def save_persona_targets(targets: list[dict]) -> None:
    _put(_path("persona_targets.json"), targets or [])


def load_persona_targets() -> list[dict]:
    data = _get(_path("persona_targets.json"), default=[]) or []
    return data if isinstance(data, list) else []


# ── Agent traces ───────────────────────────────────────────────────────────


def append_trace_event(session_id: str, event: dict) -> None:
    name = _path("agent_traces", f"{session_id}.jsonl")
    with _LOCK:
        events = _get(name, default=[], use_cache=False) or []
        if not isinstance(events, list):
            events = []
        seq = (events[-1].get("seq", 0) + 1) if events else 1
        rec = dict(event)
        rec.setdefault("ts", _now())
        rec["seq"] = seq
        events.append(rec)
        _put(name, events)


def load_trace(session_id: str) -> list[dict]:
    data = _get(_path("agent_traces", f"{session_id}.jsonl"), default=[]) or []
    return data if isinstance(data, list) else []


def tail_trace(session_id: str, since_seq: int) -> list[dict]:
    return [e for e in load_trace(session_id) if int(e.get("seq") or 0) > since_seq]


# ── Gemini call log ────────────────────────────────────────────────────────


def log_gemini_call(record: dict) -> None:
    ym = _ym(record.get("ts"))
    rec = dict(record)
    rec.setdefault("ts", _now())
    _append_jsonl(_path("gemini_calls", f"gm-{ym}.jsonl"), rec)


def gemini_usage_mtd() -> dict:
    ym = _ym()
    rows = _get(_path("gemini_calls", f"gm-{ym}.jsonl"), default=[]) or []
    by_kind: dict[str, dict[str, int]] = {}
    totals = {"calls": 0, "tokens_in": 0, "tokens_out": 0}
    if not isinstance(rows, list):
        rows = []
    for r in rows:
        kind = str(r.get("task_kind") or "unknown")
        bucket = by_kind.setdefault(
            kind, {"calls": 0, "tokens_in": 0, "tokens_out": 0}
        )
        tin = int(r.get("tokens_in") or 0)
        tout = int(r.get("tokens_out") or 0)
        bucket["calls"] += 1
        bucket["tokens_in"] += tin
        bucket["tokens_out"] += tout
        totals["calls"] += 1
        totals["tokens_in"] += tin
        totals["tokens_out"] += tout
    return {"by_task_kind": by_kind, "totals": totals, "month": ym}


# ── Drafts ─────────────────────────────────────────────────────────────────


def save_draft(draft_id: str, draft: dict) -> None:
    draft = dict(draft)
    draft["draft_id"] = draft_id
    draft.setdefault("created_at", _now())
    draft["updated_at"] = _now()
    _put(_path("drafts", f"{draft_id}.json"), draft)
    idx = _get(_path("drafts_index.json"), default=[]) or []
    if not isinstance(idx, list):
        idx = []
    entry = {
        "draft_id": draft_id,
        "recipient": draft.get("recipient") or draft.get("to"),
        "recipient_name": draft.get("recipient_name") or "",
        "title": draft.get("title")
        or draft.get("designation")
        or draft.get("recipient_title")
        or "",
        "designation": draft.get("designation")
        or draft.get("title")
        or draft.get("recipient_title")
        or "",
        "company": draft.get("company") or "",
        "subject": draft.get("subject"),
        "status": draft.get("status", "draft"),
        "updated_at": draft["updated_at"],
        "created_at": draft.get("created_at"),
        "tracking_id": draft.get("tracking_id"),
        "bulk_job_id": draft.get("bulk_job_id"),
        "confidence": draft.get("confidence"),
        "opens": draft.get("opens", 0),
        "clicks": draft.get("clicks", 0),
        "attachments": len(draft.get("attachments") or []),
        "source": draft.get("source") or draft.get("lineage_source"),
    }
    idx = [e for e in idx if e.get("draft_id") != draft_id]
    idx.insert(0, entry)
    _put(_path("drafts_index.json"), idx[:5000])


def load_draft(draft_id: str) -> dict:
    data = _get(_path("drafts", f"{draft_id}.json"), default=None)
    if not data:
        raise KeyError(f"draft not found: {draft_id}")
    return data


def list_drafts(limit: int = 50, offset: int = 0) -> list[dict]:
    idx = _get(_path("drafts_index.json"), default=[]) or []
    if not isinstance(idx, list):
        return []
    return idx[offset : offset + limit]


def delete_draft(draft_id: str) -> None:
    idx = _get(_path("drafts_index.json"), default=[]) or []
    if isinstance(idx, list):
        idx = [e for e in idx if e.get("draft_id") != draft_id]
        _put(_path("drafts_index.json"), idx)
    # leave blob; mark deleted if present
    try:
        d = load_draft(draft_id)
        d["status"] = "deleted"
        d["updated_at"] = _now()
        _put(_path("drafts", f"{draft_id}.json"), d)
    except KeyError:
        pass


# ── Chats ──────────────────────────────────────────────────────────────────


def save_chat(chat_id: str, chat: dict) -> None:
    chat = dict(chat)
    chat["chat_id"] = chat_id
    chat["updated_at"] = _now()
    _put(_path("chats", f"{chat_id}.json"), chat)
    idx = _get(_path("chats_index.json"), default=[]) or []
    if not isinstance(idx, list):
        idx = []
    entry = {
        "chat_id": chat_id,
        "title": chat.get("title") or "Chat",
        "updated_at": chat["updated_at"],
        "created_at": chat.get("created_at"),
        "message_count": len(chat.get("messages") or []),
    }
    idx = [e for e in idx if e.get("chat_id") != chat_id]
    idx.insert(0, entry)
    _put(_path("chats_index.json"), idx[:500])


def load_chat(chat_id: str) -> dict:
    data = _get(_path("chats", f"{chat_id}.json"), default=None)
    if not data:
        raise KeyError(f"chat not found: {chat_id}")
    return data


def list_chats(limit: int = 50) -> list[dict]:
    idx = _get(_path("chats_index.json"), default=[]) or []
    return idx[:limit] if isinstance(idx, list) else []


# ── Org domain cache / profiles / style / lineage / events / ZI ────────────


def load_org_domain_cache() -> dict:
    data = _get(_path("org_domain_cache.json"), default={}) or {}
    return data if isinstance(data, dict) else {}


def save_org_domain_cache(cache: dict) -> None:
    _put(_path("org_domain_cache.json"), cache or {})


def get_cached_domain(org_name: str) -> Optional[str]:
    cache = load_org_domain_cache()
    key = (org_name or "").strip().lower()
    hit = cache.get(key)
    if isinstance(hit, dict):
        return hit.get("domain")
    if isinstance(hit, str):
        return hit
    return None


def set_cached_domain(org_name: str, domain: str, org_name_resolved: str = "") -> None:
    cache = load_org_domain_cache()
    key = (org_name or "").strip().lower()
    cache[key] = {
        "domain": domain,
        "org_name": org_name_resolved or org_name,
        "updated_at": _now(),
    }
    save_org_domain_cache(cache)


def save_org_profile(domain: str, profile: dict) -> None:
    safe = (domain or "unknown").replace("/", "_")
    profile = dict(profile)
    profile["domain"] = domain
    profile["updated_at"] = _now()
    _put(_path("org_profiles", f"{safe}.json"), profile)
    idx = _get(_path("org_profiles_index.json"), default=[]) or []
    if not isinstance(idx, list):
        idx = []
    entry = {
        "domain": domain,
        "name": profile.get("name") or profile.get("org_name"),
        "updated_at": profile["updated_at"],
    }
    idx = [e for e in idx if e.get("domain") != domain]
    idx.insert(0, entry)
    _put(_path("org_profiles_index.json"), idx[:2000])


def load_org_profile(domain: str) -> Optional[dict]:
    safe = (domain or "unknown").replace("/", "_")
    return _get(_path("org_profiles", f"{safe}.json"), default=None)


def save_style_profile(profile: dict) -> None:
    _put(_path("style_profile.json"), profile or {})


def load_style_profile() -> dict:
    data = _get(_path("style_profile.json"), default={}) or {}
    return data if isinstance(data, dict) else {}


def save_lineage(new_draft_id: str, lineage: dict) -> None:
    _put(_path("lineage", f"{new_draft_id}.json"), lineage or {})


def load_lineage(new_draft_id: str) -> Optional[dict]:
    return _get(_path("lineage", f"{new_draft_id}.json"), default=None)


def append_event(event: dict) -> None:
    ym = _ym(event.get("ts"))
    rec = dict(event)
    rec.setdefault("ts", _now())
    _append_jsonl(_path("events", f"events-{ym}.jsonl"), rec)


def log_zoominfo_call(record: dict) -> None:
    ym = _ym(record.get("ts"))
    rec = dict(record)
    rec.setdefault("ts", _now())
    _append_jsonl(_path("zoominfo_calls", f"zi-{ym}.jsonl"), rec)


def zoominfo_usage_mtd() -> dict:
    ym = _ym()
    rows = _get(_path("zoominfo_calls", f"zi-{ym}.jsonl"), default=[]) or []
    credits = 0
    calls = 0
    if isinstance(rows, list):
        for r in rows:
            calls += 1
            credits += int(r.get("credits") or r.get("zi_credits") or 1)
    return {"month": ym, "calls": calls, "credits": credits}


def save_tool_registry_manifest(manifest: list[dict]) -> None:
    _put(_path("tool_registry_manifest.json"), manifest or [])


def ensure_indexes() -> None:
    """Create empty index files if missing (idempotent)."""
    defaults = {
        _path("chats_index.json"): [],
        _path("drafts_index.json"): [],
        _path("bulk_jobs_index.json"): [],
        _path("org_profiles_index.json"): [],
        _path("org_domain_cache.json"): {},
        _path("persona_targets.json"): [],
        _path("style_profile.json"): {},
        _path("tool_registry_manifest.json"): [],
    }
    for name, default in defaults.items():
        existing = _get(name, default=None)
        if existing is None:
            try:
                _put(name, default)
            except Exception as e:
                print(f"[drive_db] ensure {name}: {e}", file=sys.stderr)
