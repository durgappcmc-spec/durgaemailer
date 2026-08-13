# NOTE: DraftAgent always validates + injects before save.
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.agent.draft_agent import DraftAgent
from core.agent.planner_base import DraftAgentBudget, RowState, ContactRecord
from core.tools.base import ToolResult
from core.tools.registry import ToolRegistry, PhaseScopeError


class ScriptedGemini:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.kinds = []

    def generate(self, task_kind, prompt, **kwargs):
        self.kinds.append(task_kind)
        d = self.decisions.pop(0) if self.decisions else {"done": True, "status": "failed"}

        class R:
            parsed = d
            text = json.dumps(d)
            tokens_in = 1
            tokens_out = 1
            wall_ms = 1
            repaired = False

        return R()


def test_draft_agent_order_and_task_kinds():
    order = []

    def make(name, phase, data_fn):
        def handler(args, ctx):
            order.append(name)
            return ToolResult(ok=True, data=data_fn(args), cost={"gemini_task_kind": name} if "compose" in name or "synth" in name or "validate" in name else {})

        return name, phase, handler

    tools = [
        make("zoominfo_enrich_company", {"phase2"}, lambda a: {"company": {"name": "X"}}),
        make("web_fetch_pages", {"phase2"}, lambda a: {"pages": []}),  # empty — adapt
        make("web_find_recent_news", {"phase2"}, lambda a: {"items": [{"title": "Grant", "summary": "won"}]}),
        make("gmail_history_lookup", {"phase2"}, lambda a: {"contacts": [], "topics": []}),
        make(
            "synthesize_org_brief",
            {"phase2"},
            lambda a: {
                "org_brief": {
                    "org_name": "X",
                    "flagship_programs": [{"name": "Edu", "summary": "schools"}],
                    "recent_signals": [],
                }
            },
        ),
        make(
            "compose_hyper_personalized_email",
            {"phase2"},
            lambda a: {
                "draft": {
                    "subject": "Hi",
                    "body_html": "<p>Hi about Edu</p>",
                    "personalization_ledger": [],
                    "confidence": 0.8,
                }
            },
        ),
        make("validate_grounding", {"phase2"}, lambda a: {"ok": True, "violations": []}),
        make("inject_tracking", {"phase2"}, lambda a: {"body_html": a.get("body_html") or "<p>Hi</p><img>", "tracking_id": "tid-1"}),
        make("save_draft", {"phase2"}, lambda a: {"draft_id": "d1", "tracking_id": a.get("tracking_id") or "tid-1"}),
    ]
    reg = ToolRegistry()
    for name, phase, handler in tools:
        class T:
            pass
        t = T()
        t.name = name
        t.description = name
        t.phase_scope = phase
        t.idempotent = True
        t.cost_hint = {}
        t.handler = handler
        from pydantic import BaseModel

        class Loose(BaseModel):
            model_config = {"extra": "allow"}

        t.input_schema = Loose
        t.output_schema = Loose
        t.run = lambda inputs, ctx, h=handler: h({}, ctx)
        reg.register(t)

    def call(name, inputs, ctx):
        tool = reg.get(name)
        if ctx.phase not in tool.phase_scope:
            raise PhaseScopeError(name)
        return tool.handler(inputs or {}, ctx)

    reg.call = call  # type: ignore

    decisions = [
        {"next_tool": "zoominfo_enrich_company", "args": {}},
        {"next_tool": "web_fetch_pages", "args": {}},
        {"next_tool": "web_find_recent_news", "args": {}},
        {"next_tool": "synthesize_org_brief", "args": {}},
        {"next_tool": "compose_hyper_personalized_email", "args": {}},
        {"next_tool": "validate_grounding", "args": {}},
        {"next_tool": "inject_tracking", "args": {}},
        {"next_tool": "save_draft", "args": {"tracking_id": "tid-1"}},
        {"done": True, "status": "ready"},
    ]
    gem = ScriptedGemini(decisions)
    agent = DraftAgent(
        registry=reg,
        gemini=gem,
        budget=DraftAgentBudget(max_steps=20, max_wall_seconds=30),
        phase2_config={"intent": "partnership_outreach"},
    )
    row = RowState(
        row_id="r1",
        phase="phase2",
        input="X",
        resolved_domain="x.org",
        approved_for_phase2=True,
        contact=ContactRecord(name="A", email="a@x.org", title="CSR"),
        status="queued",
    )
    out = agent.run(row)
    assert out.status == "ready"
    assert "validate_grounding" in order
    assert order.index("validate_grounding") > order.index("compose_hyper_personalized_email")
    assert order.index("inject_tracking") < order.index("save_draft")
    assert all(k == "draft_planner" for k in gem.kinds)
    assert out.draft_id == "d1"
    assert out.tracking_id == "tid-1"
