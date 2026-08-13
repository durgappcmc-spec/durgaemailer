# NOTE: Human-supplied contact override.
from __future__ import annotations

from pydantic import BaseModel, Field

from core.tools.base import ToolContext, ToolResult


class ManualOverrideInput(BaseModel):
    name: str
    email: str | None = None
    mobile: str | None = None
    title: str | None = None
    linkedin_url: str | None = None


class ManualOverrideOutput(BaseModel):
    contact: dict = Field(default_factory=dict)
    matched_on: str = "manual_override"


class ManualOverrideContactTool:
    name = "manual_override_contact"
    description = "Apply a human-supplied contact record"
    input_schema = ManualOverrideInput
    output_schema = ManualOverrideOutput
    cost_hint = {}
    idempotent = True
    phase_scope = {"phase1"}

    def run(self, inputs: ManualOverrideInput, ctx: ToolContext) -> ToolResult:
        if not inputs.email and not inputs.mobile:
            return ToolResult(
                ok=False,
                error="email or mobile required",
                error_kind="invalid_input",
            )
        contact = {
            "name": inputs.name,
            "email": inputs.email or "",
            "mobile": inputs.mobile or "",
            "title": inputs.title or "",
            "linkedin_url": inputs.linkedin_url or "",
            "matched_on": "manual_override",
            "confidence": 1.0,
        }
        return ToolResult(
            ok=True,
            data={"contact": contact, "matched_on": "manual_override"},
        )
