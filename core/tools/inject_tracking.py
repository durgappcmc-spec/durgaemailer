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
    description = "Strip then inject hidden open pixel (no click-URL rewrite in drafts)"
    input_schema = InjectTrackingInput
    output_schema = InjectTrackingOutput
    cost_hint = {}
    idempotent = True
    phase_scope = {"phase2"}

    def run(self, inputs: InjectTrackingInput, ctx: ToolContext) -> ToolResult:
        from core.tracking import inject_tracking

        html, tid = inject_tracking(
            inputs.body_html,
            tracking_id=inputs.tracking_id,
            recipient_email=inputs.recipient_email or "unknown@example.com",
            subject=inputs.subject or "",
            register=False,
            track_clicks=False,
            track_opens=True,
        )
        return ToolResult(
            ok=True,
            data={"body_html": html, "tracking_id": tid},
        )
