# NOTE: Persist draft payload to Drive with tracking_id required.
from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from core.tools.base import ToolContext, ToolResult


class SaveDraftInput(BaseModel):
    draft: dict = Field(default_factory=dict)
    tracking_id: str
    bulk_job_id: str | None = None
    phase1_session_id: str | None = None
    phase2_session_id: str | None = None
    lineage: dict | None = None


class SaveDraftOutput(BaseModel):
    draft_id: str = ""
    tracking_id: str = ""


class SaveDraftTool:
    name = "save_draft"
    description = "Save a tracked draft to Drive"
    input_schema = SaveDraftInput
    output_schema = SaveDraftOutput
    cost_hint = {}
    idempotent = False
    phase_scope = {"phase2"}

    def run(self, inputs: SaveDraftInput, ctx: ToolContext) -> ToolResult:
        from core import drive_db
        from core.tracking import extract_tracking_id, prepare_draft_tracking

        if not inputs.tracking_id:
            return ToolResult(
                ok=False,
                error="tracking_id required — call inject_tracking first",
                error_kind="invalid_input",
            )
        draft = dict(inputs.draft or {})
        body = draft.get("body_html") or draft.get("html") or draft.get("body") or ""
        html, tid = prepare_draft_tracking(body, inputs.tracking_id)
        if extract_tracking_id(html) != tid and tid:
            html, tid = prepare_draft_tracking(html, tid)
        draft_id = draft.get("draft_id") or f"draft_{uuid.uuid4().hex[:12]}"
        payload = {
            **draft,
            "draft_id": draft_id,
            "body_html": html,
            "tracking_id": tid,
            "status": draft.get("status") or "ready",
            "bulk_job_id": inputs.bulk_job_id or ctx.job_id,
            "phase1_session_id": inputs.phase1_session_id,
            "phase2_session_id": inputs.phase2_session_id or ctx.session_id,
            "row_id": ctx.row_id,
        }
        drive_db.save_draft(draft_id, payload)
        if inputs.lineage:
            drive_db.save_lineage(draft_id, inputs.lineage)
        return ToolResult(
            ok=True,
            data={"draft_id": draft_id, "tracking_id": tid},
        )
