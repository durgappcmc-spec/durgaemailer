# NOTE: Hyper-personalized composer tool → core.hyper_drafter.
from __future__ import annotations

from pydantic import BaseModel, Field

from core.tools.base import ToolContext, ToolResult


class ComposeInput(BaseModel):
    intent: str = "partnership_outreach"
    contact: dict = Field(default_factory=dict)
    org_brief: dict = Field(default_factory=dict)
    source_email: dict | None = None
    style_profile: dict | None = None
    instructions: str = ""


class ComposeOutput(BaseModel):
    draft: dict = Field(default_factory=dict)


class ComposeHyperPersonalizedEmailTool:
    name = "compose_hyper_personalized_email"
    description = "Compose a grounded hyper-personalized email draft"
    input_schema = ComposeInput
    output_schema = ComposeOutput
    cost_hint = {"gemini_task_kind": "compose_email"}
    idempotent = False
    phase_scope = {"phase2"}

    def run(self, inputs: ComposeInput, ctx: ToolContext) -> ToolResult:
        from core.hyper_drafter import compose_email

        gemini = ctx.gemini
        try:
            draft, cost = compose_email(
                intent=inputs.intent,
                contact=inputs.contact,
                org_brief=inputs.org_brief,
                source_email=inputs.source_email,
                style_profile=inputs.style_profile,
                instructions=inputs.instructions,
                gemini=gemini,
                session_id=ctx.session_id,
                row_id=ctx.row_id,
            )
        except Exception as e:
            kind = "rate_limited" if "rate" in str(e).lower() or "429" in str(e) else "network"
            return ToolResult(ok=False, error=str(e), error_kind=kind)
        return ToolResult(ok=True, data={"draft": draft}, cost=cost or {})
