# NOTE: Sent-items similarity ranking for find_similar_sent_email tool.
from __future__ import annotations

import re
from typing import Any, Optional


def find_similar_sent(
    org_brief: dict | None = None,
    limit: int = 5,
    reference_query: str | None = None,
) -> list[dict[str, Any]]:
    """Rank past sent emails. Prefer Gmail Sent; fall back to Drive drafts/events."""
    brief = org_brief or {}
    tokens = _tokens(
        " ".join(
            filter(
                None,
                [
                    reference_query or "",
                    str(brief.get("org_name") or ""),
                    str(brief.get("mission") or ""),
                    " ".join(
                        p.get("name", "") if isinstance(p, dict) else str(p)
                        for p in (brief.get("flagship_programs") or [])
                    ),
                ],
            )
        )
    )
    candidates: list[dict[str, Any]] = []

    # Gmail Sent search
    try:
        from gmail_client.auth import gmail_service

        svc = gmail_service()
        q = "in:sent"
        if reference_query:
            q = f"in:sent {reference_query}"
        elif brief.get("org_name"):
            q = f'in:sent "{brief.get("org_name")}"'
        res = svc.users().messages().list(userId="me", q=q, maxResults=30).execute()
        for m in res.get("messages") or []:
            full = (
                svc.users()
                .messages()
                .get(
                    userId="me",
                    id=m["id"],
                    format="metadata",
                    metadataHeaders=["Subject", "To", "Date"],
                )
                .execute()
            )
            headers = {
                h["name"].lower(): h["value"]
                for h in (full.get("payload") or {}).get("headers") or []
            }
            subject = headers.get("subject") or ""
            score = _score(tokens, _tokens(subject + " " + (headers.get("to") or "")))
            candidates.append(
                {
                    "id": m["id"],
                    "subject": subject,
                    "to": headers.get("to"),
                    "date": headers.get("date"),
                    "score": score,
                    "source": "gmail_sent",
                }
            )
    except Exception:
        pass

    # Drive drafts as weak fallback
    try:
        from core import drive_db

        for d in drive_db.list_drafts(limit=50):
            subject = d.get("subject") or ""
            score = _score(tokens, _tokens(subject + " " + str(d.get("recipient") or "")))
            candidates.append(
                {
                    "id": d.get("draft_id"),
                    "subject": subject,
                    "to": d.get("recipient"),
                    "date": d.get("updated_at"),
                    "score": score * 0.7,
                    "source": "drive_draft",
                }
            )
    except Exception:
        pass

    candidates.sort(key=lambda x: x.get("score") or 0, reverse=True)
    return candidates[:limit]


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]{3,}", (text or "").lower())}


def _score(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, len(a))
