# NOTE: Prior Gmail touches for a domain.
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from core.tools.base import ToolContext, ToolResult


class GmailHistoryInput(BaseModel):
    domain: str
    limit: int = 10


class GmailHistoryOutput(BaseModel):
    contacts: list[dict] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    last_touch: str | None = None


class GmailHistoryLookupTool:
    name = "gmail_history_lookup"
    description = "Look up prior Gmail contacts/topics for a domain"
    input_schema = GmailHistoryInput
    output_schema = GmailHistoryOutput
    cost_hint = {}
    idempotent = True
    phase_scope = {"phase2"}

    def run(self, inputs: GmailHistoryInput, ctx: ToolContext) -> ToolResult:
        domain = (inputs.domain or "").strip().lower()
        if not domain:
            return ToolResult(ok=False, error="domain required", error_kind="invalid_input")
        try:
            from gmail_client.auth import gmail_service

            svc = gmail_service()
            q = f"to:*@{domain} OR from:*@{domain}"
            res = (
                svc.users()
                .messages()
                .list(userId="me", q=q, maxResults=min(inputs.limit, 20))
                .execute()
            )
            msgs = res.get("messages") or []
            contacts: list[dict] = []
            topics: list[str] = []
            last_touch = None
            for m in msgs[: inputs.limit]:
                full = (
                    svc.users()
                    .messages()
                    .get(
                        userId="me",
                        id=m["id"],
                        format="metadata",
                        metadataHeaders=["From", "To", "Subject", "Date"],
                    )
                    .execute()
                )
                headers = {
                    h["name"].lower(): h["value"]
                    for h in (full.get("payload") or {}).get("headers") or []
                }
                subjects = headers.get("subject") or ""
                if subjects:
                    topics.append(subjects[:120])
                contacts.append(
                    {
                        "from": headers.get("from"),
                        "to": headers.get("to"),
                        "subject": subjects,
                        "date": headers.get("date"),
                        "id": m["id"],
                    }
                )
                if not last_touch:
                    last_touch = headers.get("date") or datetime.now(timezone.utc).isoformat()
            return ToolResult(
                ok=True,
                data={
                    "contacts": contacts,
                    "topics": topics[:10],
                    "last_touch": last_touch,
                },
            )
        except Exception as e:
            return ToolResult(ok=False, error=str(e), error_kind="auth")
