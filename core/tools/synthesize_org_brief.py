# NOTE: Evidence bundle → OrgBrief via Gemini org_brief_synth.
from __future__ import annotations

import json

from pydantic import BaseModel, Field

from core.tools.base import ToolContext, ToolResult


class SynthesizeOrgBriefInput(BaseModel):
    evidence: dict = Field(default_factory=dict)
    org_name: str | None = None
    domain: str | None = None


class SynthesizeOrgBriefOutput(BaseModel):
    org_brief: dict = Field(default_factory=dict)


class SynthesizeOrgBriefTool:
    name = "synthesize_org_brief"
    description = "Synthesize a strict-JSON OrgBrief from evidence"
    input_schema = SynthesizeOrgBriefInput
    output_schema = SynthesizeOrgBriefOutput
    cost_hint = {"gemini_task_kind": "org_brief_synth"}
    idempotent = False
    phase_scope = {"phase2"}

    def run(self, inputs: SynthesizeOrgBriefInput, ctx: ToolContext) -> ToolResult:
        gemini = ctx.gemini
        if gemini is None:
            from core.agent.gemini_client import get_gemini_client

            gemini = get_gemini_client()
        prompt = (
            "Synthesize an OrgBrief as strict JSON with keys: "
            "org_name, domain, mission, flagship_programs (list of {name, summary}), "
            "recent_signals (list of {title, summary, url?}), audience, tone_notes, "
            "evidence_ids (list). Only use facts present in the evidence. "
            "If a field is unknown, use null or [].\n\n"
            f"org_name={inputs.org_name}\ndomain={inputs.domain}\n"
            f"evidence=\n{json.dumps(inputs.evidence, default=str)[:40000]}"
        )
        try:
            resp = gemini.generate(
                "org_brief_synth",
                prompt,
                session_id=ctx.session_id,
                row_id=ctx.row_id,
                expect_json=True,
            )
        except Exception as e:
            kind = "rate_limited" if "rate" in str(e).lower() or "429" in str(e) else "network"
            return ToolResult(ok=False, error=str(e), error_kind=kind)

        brief = resp.parsed if isinstance(resp.parsed, dict) else {"raw": resp.text}
        brief.setdefault("org_name", inputs.org_name)
        brief.setdefault("domain", inputs.domain)
        # Persist profile
        try:
            from core import drive_db

            if inputs.domain:
                drive_db.save_org_profile(inputs.domain, brief)
        except Exception:
            pass
        return ToolResult(
            ok=True,
            data={"org_brief": brief},
            cost={
                "gemini_tokens_in": resp.tokens_in,
                "gemini_tokens_out": resp.tokens_out,
                "gemini_task_kind": "org_brief_synth",
                "wall_ms": resp.wall_ms,
            },
        )
