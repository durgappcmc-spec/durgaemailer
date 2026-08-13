# NOTE: Persist chat / prospects / memory across rebuilds — Drive is source of truth.
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
_MAX_CHAT_MSGS = 40
_LOCAL_DIR = _DATA / "durable"
_ENSURED_TABS: set[str] = set()
_APPSTATE_CACHE: Optional[dict[str, Any]] = None
_APPSTATE_CACHE_AT = 0.0
_APPSTATE_TTL = 60.0
_sheets_lock = threading.Lock()

# Large blobs → Google Drive (avoids Sheets cell rewrites + Render OOM)
_DRIVE_KEYS = frozenset(
    {"memory_rows", "prospect_list", "chat_messages", "session_extras", "contact_aliases"}
)
_DRIVE_FILE = {
    "memory_rows": "relay_memory.json",
    "prospect_list": "relay_prospects.json",
    "chat_messages": "relay_chat.json",
    "session_extras": "relay_session.json",
    "contact_aliases": "relay_aliases.json",
}


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


def _payload_len(payload: Any) -> int:
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        return len(payload)
    if payload is None:
        return 0
    return 1


def _is_empty_payload(payload: Any) -> bool:
    if payload is None:
        return True
    if isinstance(payload, (list, dict)):
        return len(payload) == 0
    return False


def _local_is_miss(payload: Any) -> bool:
    """Empty list/dict after a Render rebuild must not block Drive restore."""
    return payload is None or _is_empty_payload(payload)


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


def _protect_drive_upload(key: str, payload: Any, *, allow_empty: bool) -> Any:
    """Never clobber richer Drive data with empty/sparse post-deploy state.

    Returns the payload to upload, or None to skip the Drive upload.
    """
    if key not in _DRIVE_KEYS:
        return payload
    if _is_empty_payload(payload) and not allow_empty:
        print(
            f"[durable] skip Drive upload for empty {key} (protect cloud data)",
            file=sys.stderr,
        )
        return None
    if allow_empty:
        return payload
    if key not in ("prospect_list", "memory_rows", "chat_messages", "contact_aliases"):
        return payload
    if not isinstance(payload, (list, dict)):
        return payload
    try:
        from core.drive_store import download_json

        existing = download_json(_DRIVE_FILE.get(key, f"{key}.json"))
    except Exception as e:
        print(f"[durable] drive pre-check {key} failed: {e}", file=sys.stderr)
        return payload
    if existing is None:
        return payload
    if type(existing) is not type(payload):
        return payload
    old_n = _payload_len(existing)
    new_n = _payload_len(payload)
    # Accidental wipe: sparse local after rebuild uploading over a full Drive blob
    if old_n > 0 and new_n < max(3, int(old_n * 0.5)):
        if key == "prospect_list" and isinstance(existing, list) and isinstance(payload, list):
            merged = _merge_prospect_lists(existing, payload)
            print(
                f"[durable] merge Drive {key}: cloud={old_n} local={new_n} → {len(merged)}",
                file=sys.stderr,
            )
            _save_local(key, merged)
            return merged
        if key == "memory_rows" and isinstance(existing, list) and isinstance(payload, list):
            merged = _merge_memory_rows(existing, payload)
            print(
                f"[durable] merge Drive {key}: cloud={old_n} local={new_n} → {len(merged)}",
                file=sys.stderr,
            )
            _save_local(key, merged)
            return merged
        if key == "contact_aliases" and isinstance(existing, dict) and isinstance(payload, dict):
            merged = {**existing, **payload}
            print(
                f"[durable] merge Drive {key}: cloud={old_n} local={new_n} → {len(merged)}",
                file=sys.stderr,
            )
            _save_local(key, merged)
            return merged
        # chat_messages: keep richer cloud until hydrate succeeds
        print(
            f"[durable] skip Drive upload for sparse {key} "
            f"(cloud={old_n} local={new_n})",
            file=sys.stderr,
        )
        return None
    return payload


def _merge_prospect_lists(
    existing: list[Any], incoming: list[Any]
) -> list[dict[str, Any]]:
    def _key(p: dict[str, Any]) -> str:
        email = str(p.get("email") or "").strip().lower()
        if email and "@" in email:
            return f"email:{email}"
        sid = str(p.get("source_id") or "").strip().lower()
        if sid:
            return f"sid:{sid}"
        name = str(p.get("name") or "").strip().lower()
        company = str(
            p.get("company") or p.get("organization") or p.get("org") or ""
        ).strip().lower()
        return f"nc:{name}|{company}"

    by_key: dict[str, dict[str, Any]] = {}
    for row in list(existing) + list(incoming):
        if not isinstance(row, dict):
            continue
        by_key[_key(row)] = {**by_key.get(_key(row), {}), **row}
    return list(by_key.values())[-1000:]


def _merge_memory_rows(existing: list[Any], incoming: list[Any]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for row in list(existing) + list(incoming):
        if not isinstance(row, dict):
            continue
        rid = str(row.get("id") or row.get("source_id") or "")
        if not rid:
            rid = str(hash(json.dumps(row, sort_keys=True, default=str)))
        by_id[rid] = row
    return list(by_id.values())[-1500:]


def save_json_blob(
    key: str,
    payload: Any,
    *,
    sync_sheets: bool = True,
    allow_empty: bool = False,
) -> bool:
    """Local write is immediate; large keys sync to Google Drive (not Sheets)."""
    _save_local(key, payload)
    if not sync_sheets:
        return True
    if key in _DRIVE_KEYS:
        drive_ok = False
        to_upload: Any = payload
        try:
            from core.drive_store import upload_json

            to_upload = _protect_drive_upload(key, payload, allow_empty=allow_empty)
            if to_upload is None:
                drive_ok = True  # intentionally skipped (protect cloud)
            else:
                drive_ok = bool(
                    upload_json(_DRIVE_FILE.get(key, f"{key}.json"), to_upload)
                )
        except Exception as e:
            print(f"[durable] drive save {key} failed: {e}", file=sys.stderr)
            drive_ok = False
        # Second durable store: Sheets AppState (survives if Drive folder drifts)
        if key in ("prospect_list", "memory_rows", "contact_aliases", "chat_messages"):
            try:
                if to_upload is not None and (
                    not _is_empty_payload(to_upload) or allow_empty
                ):
                    text = json.dumps(to_upload, ensure_ascii=False, default=str)
                    sheets_ok = _write_appstate_key(key, text)
                    if sheets_ok:
                        print(
                            f"[durable] mirrored {key} to Sheets AppState",
                            file=sys.stderr,
                        )
            except Exception as e:
                print(f"[durable] sheets mirror {key} failed: {e}", file=sys.stderr)
        return drive_ok
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
        return _write_appstate_key(key, text)
    except Exception as e:
        print(f"[durable] save {key} failed: {e}", file=sys.stderr)
        return False


def save_json_blob_async(
    key: str, payload: Any, *, allow_empty: bool = False
) -> None:
    """Non-blocking Drive/Sheets backup after local save."""
    # Reuse sync path so Drive + Sheets mirror stay consistent
    def _run() -> None:
        try:
            save_json_blob(key, payload, allow_empty=allow_empty)
        except Exception as e:
            print(f"[durable] async save {key} failed: {e}", file=sys.stderr)

    _save_local(key, payload)
    threading.Thread(target=_run, daemon=True, name=f"durable-{key}").start()


def load_json_blob(key: str, *, allow_sheets: bool = True) -> Optional[Any]:
    """Prefer non-empty local file. Empty/sparse local falls through to Drive."""
    local = _load_local(key)
    if not allow_sheets:
        return local
    cloud: Any = None
    if key in _DRIVE_KEYS:
        try:
            from core.drive_store import download_json

            cloud = download_json(_DRIVE_FILE.get(key, f"{key}.json"))
        except Exception as e:
            print(f"[durable] drive load {key} failed: {e}", file=sys.stderr)
    if cloud is not None and not _is_empty_payload(cloud):
        # Prefer Drive when local is missing, empty, or much smaller (post-deploy wipe)
        if _local_is_miss(local):
            _save_local(key, cloud)
            return cloud
        if (
            isinstance(cloud, list)
            and isinstance(local, list)
            and len(cloud) > max(len(local), 2)
            and len(local) < max(3, int(len(cloud) * 0.5))
        ):
            print(
                f"[durable] prefer Drive {key}: cloud={len(cloud)} local={len(local)}",
                file=sys.stderr,
            )
            _save_local(key, cloud)
            return cloud
        if (
            isinstance(cloud, dict)
            and isinstance(local, dict)
            and len(cloud) > len(local)
            and len(local) < max(2, int(len(cloud) * 0.5))
        ):
            _save_local(key, cloud)
            return cloud
    if local is not None and not _local_is_miss(local):
        return local
    if cloud is not None:
        if not _is_empty_payload(cloud):
            _save_local(key, cloud)
        return cloud
    try:
        flat = _read_appstate_map()
        raw = flat.get(key)
        if not raw:
            return local
        data = json.loads(raw)
        if not _is_empty_payload(data):
            print(f"[durable] restored {key} from Sheets AppState", file=sys.stderr)
            _save_local(key, data)
            return data
        return local if local is not None else data
    except Exception as e:
        print(f"[durable] sheets load {key} failed: {e}", file=sys.stderr)
        return local


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
    # Explicit wipe — allow empty Drive upload
    save_json_blob_async("chat_messages", [], allow_empty=True)
    return True


def save_session_extras(
    *,
    prospects: Optional[list] = None,
    mailbox: Optional[list] = None,
) -> bool:
    payload = _load_local("session_extras") or {}
    if not isinstance(payload, dict):
        payload = {}
    # Prefer cloud base when local extras are empty (post-deploy)
    if not payload:
        try:
            cloud = load_json_blob("session_extras", allow_sheets=True)
            if isinstance(cloud, dict) and cloud:
                payload = dict(cloud)
        except Exception:
            pass
    if prospects is not None:
        slim = []
        for p in (prospects or [])[:80]:
            if not isinstance(p, dict):
                continue
            slim.append(
                {
                    k: p.get(k)
                    for k in (
                        "email",
                        "name",
                        "title",
                        "company",
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
        for m in (mailbox or [])[:40]:
            if not isinstance(m, dict):
                continue
            slim_m.append(
                {
                    k: m.get(k)
                    for k in (
                        "id",
                        "from",
                        "to",
                        "subject",
                        "snippet",
                        "date",
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
    # Keep cloud memory small on free Render
    payload = rows[-1500:]
    return save_json_blob("memory_rows", payload, sync_sheets=True)


def save_memory_rows_async(rows: list[dict[str, Any]]) -> None:
    save_json_blob_async("memory_rows", rows[-1500:])


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
    """One Drive/Sheets round-trip only when local chat is empty (post-deploy)."""
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


def hydrate_prospects_from_drive() -> int:
    """Force-pull prospect list from Drive into local cache. Returns contact count."""
    try:
        data = load_json_blob("prospect_list", allow_sheets=True)
        if isinstance(data, list):
            return len([r for r in data if isinstance(r, dict)])
    except Exception as e:
        print(f"[durable] prospect hydrate failed: {e}", file=sys.stderr)
    return 0


def pull_sheets_into_local_async() -> None:
    """Background: fill local cache from Drive/Sheets when local keys are empty."""

    def _run() -> None:
        try:
            for key in ("chat_messages", "session_extras", "memory_rows", "prospect_list"):
                local = _load_local(key)
                if local is not None and not _local_is_miss(local):
                    continue
                load_json_blob(key, allow_sheets=True)
        except Exception as e:
            print(f"[durable] background pull failed: {e}", file=sys.stderr)

    threading.Thread(target=_run, daemon=True, name="durable-pull").start()
