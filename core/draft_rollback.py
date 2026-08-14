# NOTE: Remember Gmail drafts created this session so Chat can roll them back.
from __future__ import annotations

from typing import Any

ROLLBACK_KEY = "gmail_draft_rollback_stack"


def remember_created(session: dict, draft_ids: list[str], *, note: str = "") -> None:
    ids = [str(d).removeprefix("gmail:") for d in (draft_ids or []) if d]
    if not ids:
        return
    stack = session.setdefault(ROLLBACK_KEY, [])
    stack.append({"draft_ids": ids, "note": note})


def can_rollback(session: dict) -> bool:
    return bool(session.get(ROLLBACK_KEY))


def rollback_last(session: dict) -> dict[str, Any]:
    stack = session.get(ROLLBACK_KEY) or []
    if not stack:
        return {"error": "nothing to roll back"}
    item = stack.pop()
    deleted: list[str] = []
    errors: list[str] = []
    try:
        from gmail_client.drafts import delete_gmail_draft
    except Exception as e:
        return {"error": str(e), "draft_ids": item.get("draft_ids") or []}
    for did in item.get("draft_ids") or []:
        res = delete_gmail_draft(did)
        if res.get("error"):
            errors.append(f"{did}: {res['error']}")
        else:
            deleted.append(did)
    return {
        "ok": not errors,
        "deleted": deleted,
        "errors": errors,
        "note": item.get("note") or "",
    }
