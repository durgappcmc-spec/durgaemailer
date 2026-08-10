# NOTE: Persist chat / prospects / memory across rebuilds — local-first, Sheets backup.
from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from config import _DATA
from core.google_sheets import ensure_tab, sheet_id, sheets_service

APP_STATE_TAB = "AppState"
_CHUNK = 45000
_MAX_CHAT_MSGS = 80
_LOCAL_DIR = _DATA / "durable"
_ENSURED_TABS: set[str] = set()
_APPSTATE_CACHE: Optional[dict[str, Any]] = None
_APPSTATE_CACHE_AT = 0.0
_APPSTATE_TTL = 60.0
_sheets_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _local_path(key: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in key)
    return _LOCAL_DIR / f"{safe}.json"


def _save_local(key: str, payload: Any) -> bool:
    try:
        _LOCAL_DIR.mkdir(parents=True, exist_ok=True)
        _local_path(key).write_text(
            json.dumps(payload, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        return True
    except Exception as e:
        print(f"[durable] local save {key} failed: {e}", file=sys.stderr)
        return False


def _load_local(key: str) -> Optional[Any]:
    try:
        path = _local_path(key)
        if not path.is_file() or path.stat().st_size == 0:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[durable] local load {key} failed: {e}", file=sys.stderr)
        return None


def _ensure_app_state() -> bool:
    if APP_STATE_TAB in _ENSURED_TABS:
        return True
    ok = ensure_tab(APP_STATE_TAB, ["key", "chunk", "value", "updated_at"])
    if ok:
        _ENSURED_TABS.add(APP_STATE_TAB)
    return ok


def _read_appstate_map(*, force: bool = False) -> dict[str, str]:
    """One Sheets read → {key: joined_json_text}. Cached briefly."""
    global _APPSTATE_CACHE, _APPSTATE_CACHE_AT
    now = time.time()
    if (
        not force
        and _APPSTATE_CACHE is not None
        and (now - _APPSTATE_CACHE_AT) < _APPSTATE_TTL
    ):
        return dict(_APPSTATE_CACHE)

    sid = sheet_id()
    svc = sheets_service()
    out: dict[str, list[tuple[int, str]]] = {}
    if not sid or not svc:
        _APPSTATE_CACHE = {}
        _APPSTATE_CACHE_AT = now
        return {}
    try:
        _ensure_app_state()
        data = (
            svc.spreadsheets()
            .values()
            .get(spreadsheetId=sid, range=f"'{APP_STATE_TAB}'!A:D")
            .execute()
            .get("values")
            or []
        )
        for row in data[1:]:
            if len(row) < 3:
                continue
            key = str(row[0])
            try:
                idx = int(row[1])
            except Exception:
                idx = 0
            out.setdefault(key, []).append((idx, row[2] or ""))
        flat: dict[str, str] = {}
        for key, parts in out.items():
            parts.sort(key=lambda x: x[0])
            flat[key] = "".join(p for _, p in parts)
        _APPSTATE_CACHE = flat
        _APPSTATE_CACHE_AT = now
        return dict(flat)
    except Exception as e:
        print(f"[durable] appstate read failed: {e}", file=sys.stderr)
        return {}


def _write_appstate_key(key: str, text: str) -> bool:
    """Update one key in AppState without rewriting unrelated keys when possible."""
    global _APPSTATE_CACHE, _APPSTATE_CACHE_AT
    sid = sheet_id()
    svc = sheets_service()
    if not sid or not svc:
        return False
    if not _ensure_app_state():
        return False
    try:
        with _sheets_lock:
            data = (
                svc.spreadsheets()
                .values()
                .get(spreadsheetId=sid, range=f"'{APP_STATE_TAB}'!A:D")
                .execute()
                .get("values")
                or []
            )
            header = (
                data[0]
                if data
                else ["key", "chunk", "value", "updated_at"]
            )
            keep: list[list[str]] = [header]
            for row in data[1:]:
                if not row or str(row[0]) == key:
                    continue
                keep.append(row)
            chunks = [text[i : i + _CHUNK] for i in range(0, max(1, len(text)), _CHUNK)]
            ts = _now()
            for i, chunk in enumerate(chunks):
                keep.append([key, str(i), chunk, ts])
            svc.spreadsheets().values().clear(
                spreadsheetId=sid, range=f"'{APP_STATE_TAB}'"
            ).execute()
            svc.spreadsheets().values().update(
                spreadsheetId=sid,
                range=f"'{APP_STATE_TAB}'!A1",
                valueInputOption="RAW",
                body={"values": keep},
            ).execute()
            # refresh cache entry
            flat = _read_appstate_map(force=True)
            flat[key] = text
            _APPSTATE_CACHE = flat
            _APPSTATE_CACHE_AT = time.time()
        return True
    except Exception as e:
        print(f"[durable] sheets write {key} failed: {e}", file=sys.stderr)
        return False


def save_json_blob(key: str, payload: Any, *, sync_sheets: bool = True) -> bool:
    """Local write is immediate; Sheets sync is optional / background-friendly."""
    _save_local(key, payload)
    if not sync_sheets:
        return True
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
        return _write_appstate_key(key, text)
    except Exception as e:
        print(f"[durable] save {key} failed: {e}", file=sys.stderr)
        return False


def save_json_blob_async(key: str, payload: Any) -> None:
    """Non-blocking Sheets backup after local save."""
    _save_local(key, payload)

    def _run() -> None:
        try:
            text = json.dumps(payload, ensure_ascii=False, default=str)
            _write_appstate_key(key, text)
        except Exception as e:
            print(f"[durable] async save {key} failed: {e}", file=sys.stderr)

    threading.Thread(target=_run, daemon=True, name=f"durable-{key}").start()


def load_json_blob(key: str, *, allow_sheets: bool = True) -> Optional[Any]:
    """Prefer local file (fast). Sheets only if local missing."""
    local = _load_local(key)
    if local is not None:
        return local
    if not allow_sheets:
        return None
    try:
        flat = _read_appstate_map()
        raw = flat.get(key)
        if not raw:
            return None
        data = json.loads(raw)
        _save_local(key, data)
        return data
    except Exception as e:
        print(f"[durable] load {key} failed: {e}", file=sys.stderr)
        return None


def _slim_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    slim: list[dict[str, Any]] = []
    for m in (messages or [])[-_MAX_CHAT_MSGS:]:
        slim.append(
            {
                "role": m.get("role"),
                "content": m.get("content") or "",
                "files": m.get("files") or [],
                "meta": {
                    k: (m.get("meta") or {}).get(k)
                    for k in ("routing", "cancelled", "need_file")
                    if (m.get("meta") or {}).get(k) is not None
                }
                or None,
            }
        )
    return slim


def save_chat_messages(messages: list[dict[str, Any]]) -> bool:
    slim = _slim_messages(messages)
    save_json_blob_async("chat_messages", slim)
    return True


def load_chat_messages(*, allow_sheets: bool = True) -> list[dict[str, Any]]:
    data = load_json_blob("chat_messages", allow_sheets=allow_sheets)
    return data if isinstance(data, list) else []


def clear_chat_messages() -> bool:
    save_json_blob_async("chat_messages", [])
    return True


def save_session_extras(
    *,
    prospects: Optional[list] = None,
    mailbox: Optional[list] = None,
) -> bool:
    payload = _load_local("session_extras") or {}
    if not isinstance(payload, dict):
        payload = {}
    if prospects is not None:
        slim = []
        for p in (prospects or [])[:200]:
            if not isinstance(p, dict):
                continue
            slim.append(
                {
                    k: p.get(k)
                    for k in (
                        "email",
                        "name",
                        "full_name",
                        "title",
                        "organization",
                        "company",
                        "org",
                        "phone",
                        "mobile",
                        "source",
                    )
                    if p.get(k)
                }
            )
        payload["last_prospects"] = slim
    if mailbox is not None:
        slim_m = []
        for m in (mailbox or [])[:100]:
            if not isinstance(m, dict):
                continue
            slim_m.append(
                {
                    k: m.get(k)
                    for k in (
                        "id",
                        "thread_id",
                        "from",
                        "to",
                        "subject",
                        "snippet",
                        "date",
                        "label",
                    )
                    if m.get(k)
                }
            )
        payload["last_mailbox"] = slim_m
    payload["updated_at"] = _now()
    save_json_blob_async("session_extras", payload)
    return True


def load_session_extras(*, allow_sheets: bool = True) -> dict[str, Any]:
    data = load_json_blob("session_extras", allow_sheets=allow_sheets)
    return data if isinstance(data, dict) else {}


def save_memory_rows(rows: list[dict[str, Any]]) -> bool:
    # Memory can be large — local always; Sheets throttled by caller
    payload = rows[-5000:]
    return save_json_blob("memory_rows", payload, sync_sheets=True)


def save_memory_rows_async(rows: list[dict[str, Any]]) -> None:
    save_json_blob_async("memory_rows", rows[-5000:])


def load_memory_rows(*, allow_sheets: bool = True) -> list[dict[str, Any]]:
    data = load_json_blob("memory_rows", allow_sheets=allow_sheets)
    return data if isinstance(data, list) else []


def hydrate_session_fast(session_state: Any) -> None:
    """Restore from local disk instantly; cold-start may do one chat Sheets fetch."""
    if session_state.get("_durable_hydrated"):
        return
    session_state["_durable_hydrated"] = True
    try:
        if not session_state.get("messages"):
            restored = load_chat_messages(allow_sheets=False)
            if restored:
                session_state["messages"] = restored
        extras = load_session_extras(allow_sheets=False)
        if extras.get("last_prospects") and not session_state.get("last_prospects"):
            session_state["last_prospects"] = extras["last_prospects"]
        if extras.get("last_mailbox") and not session_state.get("last_mailbox"):
            session_state["last_mailbox"] = extras["last_mailbox"]
    except Exception as e:
        print(f"[durable] fast hydrate failed: {e}", file=sys.stderr)


def hydrate_chat_from_sheets_if_empty(session_state: Any) -> bool:
    """One Sheets round-trip only when local chat is empty (post-deploy cold start)."""
    if session_state.get("messages"):
        return False
    if session_state.get("_chat_sheets_tried"):
        return False
    session_state["_chat_sheets_tried"] = True
    try:
        restored = load_chat_messages(allow_sheets=True)
        if restored:
            session_state["messages"] = restored
            extras = load_session_extras(allow_sheets=True)
            if extras.get("last_prospects") and not session_state.get("last_prospects"):
                session_state["last_prospects"] = extras["last_prospects"]
            if extras.get("last_mailbox") and not session_state.get("last_mailbox"):
                session_state["last_mailbox"] = extras["last_mailbox"]
            return True
    except Exception as e:
        print(f"[durable] sheets chat hydrate failed: {e}", file=sys.stderr)
    return False


def pull_sheets_into_local_async() -> None:
    """Background: fill local cache from Sheets if local keys are empty."""

    def _run() -> None:
        try:
            for key in ("chat_messages", "session_extras", "memory_rows"):
                if _load_local(key) is not None:
                    continue
                flat = _read_appstate_map()
                raw = flat.get(key)
                if not raw:
                    continue
                data = json.loads(raw)
                _save_local(key, data)
        except Exception as e:
            print(f"[durable] background pull failed: {e}", file=sys.stderr)

    threading.Thread(target=_run, daemon=True, name="durable-pull").start()
