# NOTE: Persist chat / prospects / memory JSON across Render rebuilds via Google Sheets.
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from core.google_sheets import ensure_tab, sheet_id, sheets_service

APP_STATE_TAB = "AppState"
_CHUNK = 45000  # under Sheets 50k cell limit
_MAX_CHAT_MSGS = 120


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def save_json_blob(key: str, payload: Any) -> bool:
    """Store JSON under AppState as chunked rows (key, chunk_i, value)."""
    sid = sheet_id()
    svc = sheets_service()
    if not sid or not svc:
        return False
    if not ensure_tab(APP_STATE_TAB, ["key", "chunk", "value", "updated_at"]):
        return False
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
        chunks = [text[i : i + _CHUNK] for i in range(0, max(1, len(text)), _CHUNK)]
        # Read existing to delete prior key rows
        data = (
            svc.spreadsheets()
            .values()
            .get(spreadsheetId=sid, range=f"'{APP_STATE_TAB}'!A:D")
            .execute()
            .get("values")
            or []
        )
        keep: list[list[str]] = []
        if data:
            keep.append(data[0] if data[0] else ["key", "chunk", "value", "updated_at"])
            for row in data[1:]:
                if not row or str(row[0]) == key:
                    continue
                keep.append(row)
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
        return True
    except Exception as e:
        print(f"[durable] save {key} failed: {e}", file=sys.stderr)
        return False


def load_json_blob(key: str) -> Optional[Any]:
    sid = sheet_id()
    svc = sheets_service()
    if not sid or not svc:
        return None
    try:
        ensure_tab(APP_STATE_TAB, ["key", "chunk", "value", "updated_at"])
        data = (
            svc.spreadsheets()
            .values()
            .get(spreadsheetId=sid, range=f"'{APP_STATE_TAB}'!A:D")
            .execute()
            .get("values")
            or []
        )
        parts: list[tuple[int, str]] = []
        for row in data[1:]:
            if len(row) < 3:
                continue
            if str(row[0]) != key:
                continue
            try:
                idx = int(row[1])
            except Exception:
                idx = 0
            parts.append((idx, row[2] or ""))
        if not parts:
            return None
        parts.sort(key=lambda x: x[0])
        text = "".join(p for _, p in parts)
        return json.loads(text)
    except Exception as e:
        print(f"[durable] load {key} failed: {e}", file=sys.stderr)
        return None


def save_chat_messages(messages: list[dict[str, Any]]) -> bool:
    slim: list[dict[str, Any]] = []
    for m in (messages or [])[-_MAX_CHAT_MSGS:]:
        slim.append(
            {
                "role": m.get("role"),
                "content": m.get("content") or "",
                "files": m.get("files") or [],
                # Drop huge meta blobs; keep routing hint
                "meta": {
                    k: (m.get("meta") or {}).get(k)
                    for k in ("routing", "cancelled", "need_file")
                    if (m.get("meta") or {}).get(k) is not None
                }
                or None,
            }
        )
    return save_json_blob("chat_messages", slim)


def load_chat_messages() -> list[dict[str, Any]]:
    data = load_json_blob("chat_messages")
    return data if isinstance(data, list) else []


def clear_chat_messages() -> bool:
    return save_json_blob("chat_messages", [])


def save_session_extras(
    *,
    prospects: Optional[list] = None,
    mailbox: Optional[list] = None,
) -> bool:
    payload = load_json_blob("session_extras") or {}
    if not isinstance(payload, dict):
        payload = {}
    if prospects is not None:
        # Cap size — keep email/name/org essentials
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
        for m in (mailbox or [])[:150]:
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
    return save_json_blob("session_extras", payload)


def load_session_extras() -> dict[str, Any]:
    data = load_json_blob("session_extras")
    return data if isinstance(data, dict) else {}


def save_memory_rows(rows: list[dict[str, Any]]) -> bool:
    return save_json_blob("memory_rows", rows[-5000:])


def load_memory_rows() -> list[dict[str, Any]]:
    data = load_json_blob("memory_rows")
    return data if isinstance(data, list) else []
