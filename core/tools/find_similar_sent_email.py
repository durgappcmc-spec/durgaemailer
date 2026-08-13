# NOTE: Rank similar past sent emails.
from __future__ import annotations

from pydantic import BaseModel, Field

from core.tools.base import ToolContext, ToolResult


class FindSimilarInput(BaseModel):
    new_org_brief: dict = Field(default_factory=dict)
    limit: int = 5
    reference_query: str | None = None


class FindSimilarOutput(BaseModel):
    emails: list[dict] = Field(default_factory=list)


class FindSimilarSentEmailTool:
    name = "find_similar_sent_email"
    description = "Rank past sent emails similar to the new org brief"
    input_schema = FindSimilarInput
    output_schema = FindSimilarOutput
    cost_hint = {}
    idempotent = True
    phase_scope = {"phase2"}

    def run(self, inputs: FindSimilarInput, ctx: ToolContext) -> ToolResult:
        from core.sent_items import find_similar_sent

        try:
            emails = find_similar_sent(
                org_brief=inputs.new_org_brief,
                limit=inputs.limit,
                reference_query=inputs.reference_query,
            )
        except Exception as e:
            return ToolResult(ok=False, error=str(e), error_kind="network")
        return ToolResult(ok=True, data={"emails": emails})
