# NOTE: Grounding validation via Gemini grounding_check + heuristic.
from __future__ import annotations

from pydantic import BaseModel, Field

from core.tools.base import ToolContext, ToolResult


class ValidateGroundingInput(BaseModel):
    draft: dict = Field(default_factory=dict)
    org_brief: dict = Field(default_factory=dict)
    source_email: dict | None = None
    style_profile: dict | None = None


class ValidateGroundingOutput(BaseModel):
    ok: bool = False
    violations: list[dict] = Field(default_factory=list)


class ValidateGroundingTool:
    name = "validate_grounding"
    description = "Validate draft claims against org brief evidence"
    input_schema = ValidateGroundingInput
    output_schema = ValidateGroundingOutput
    cost_hint = {"gemini_task_kind": "grounding_check"}
    idempotent = True
    phase_scope = {"phase2"}

    def run(self, inputs: ValidateGroundingInput, ctx: ToolContext) -> ToolResult:
        from core.hyper_drafter import validate_grounding

        gemini = ctx.gemini
        try:
            result, cost = validate_grounding(
                draft=inputs.draft,
                org_brief=inputs.org_brief,
                source_email=inputs.source_email,
                style_profile=inputs.style_profile,
                gemini=gemini,
                session_id=ctx.session_id,
                row_id=ctx.row_id,
            )
        except Exception as e:
            kind = "rate_limited" if "rate" in str(e).lower() or "429" in str(e) else "network"
            return ToolResult(ok=False, error=str(e), error_kind=kind)
        return ToolResult(
            ok=True,
            data={"ok": bool(result.get("ok")), "violations": result.get("violations") or []},
            cost=cost or {},
        )
