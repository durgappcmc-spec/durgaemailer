# NOTE: Pause row and surface a question to the human.
from __future__ import annotations

from pydantic import BaseModel, Field

from core.tools.base import ToolContext, ToolResult


class AskHumanInput(BaseModel):
    question: str
    options: list[str] = Field(default_factory=list)


class AskHumanOutput(BaseModel):
    paused: bool = True
    question: str = ""


class AskHumanTool:
    name = "ask_human"
    description = "Pause the row and ask the human a question"
    input_schema = AskHumanInput
    output_schema = AskHumanOutput
    cost_hint = {}
    idempotent = False
    phase_scope = {"phase1", "phase2"}

    def run(self, inputs: AskHumanInput, ctx: ToolContext) -> ToolResult:
        return ToolResult(
            ok=True,
            data={
                "paused": True,
                "question": inputs.question,
                "options": inputs.options,
                "status": "awaiting_human",
            },
        )
