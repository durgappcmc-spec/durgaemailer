# NOTE: Idempotent tracking inject tool.
from __future__ import annotations

from pydantic import BaseModel

from core.tools.base import ToolContext, ToolResult


class InjectTrackingInput(BaseModel):
    body_html: str
    tracking_id: str | None = None
    recipient_email: str = ""
    subject: str = ""


class InjectTrackingOutput(BaseModel):
    body_html: str = ""
    tracking_id: str = ""


class InjectTrackingTool:
    name = "inject_tracking"
    description = "Assign a tracking id for drafts without a live open pixel"
    input_schema = InjectTrackingInput
    output_schema = InjectTrackingOutput
    cost_hint = {}
    idempotent = True
    phase_scope = {"phase2"}

    def run(self, inputs: InjectTrackingInput, ctx: ToolContext) -> ToolResult:
        from core.tracking import prepare_draft_tracking

        html, tid = prepare_draft_tracking(
            inputs.body_html,
            inputs.tracking_id,
        )
        return ToolResult(
            ok=True,
            data={"body_html": html, "tracking_id": tid},
        )
