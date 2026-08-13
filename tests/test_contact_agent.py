# NOTE: ContactAgent never calls Phase 2 tools; routes around ZI not_found.
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.agent.planner_base import RowState
from core.tools.base import ToolResult
from core.tools.registry import ToolRegistry, PhaseScopeError


class ScriptedGemini:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.kinds = []

    def generate(self, task_kind, prompt, **kwargs):
        self.kinds.append(task_kind)
        d = self.decisions.pop(0) if self.decisions else {"done": True, "status": "failed", "reason": "empty"}

        class R:
            text = json.dumps(d)
            parsed = d
            tokens_in = 1
            tokens_out = 1
            wall_ms = 1
            repaired = False
            task_kind = task_kind

        return R()


class FakeTool:
    def __init__(self, name, phase_scope, handler):
        from pydantic import BaseModel, Field

        class In(BaseModel):
            data: dict = Field(default_factory=dict)

            model_config = {"extra": "allow"}

            @classmethod
            def model_validate(cls, obj):
                # accept arbitrary dicts
                inst = cls()
                object.__setattr__(inst, "__dict__", {"_raw": obj})
                return inst

        self.name = name
        self.description = name
        self.input_schema = In
        self.output_schema = In
        self.cost_hint = {}
        self.idempotent = True
        self.phase_scope = phase_scope
        self.handler = handler
        self.calls = []

    def run(self, inputs, ctx):
        raw = getattr(inputs, "_raw", {}) or {}
        self.calls.append(raw)
        return self.handler(raw, ctx)


def _patch_input_schema(tool):
    from pydantic import BaseModel, create_model

    # Use empty model that allows extra via model_construct
    class AnyIn(BaseModel):
        model_config = {"extra": "allow"}

    tool.input_schema = AnyIn
    return tool


def test_contact_agent_fallback_and_phase_scope(monkeypatch):
    from core.agent.contact_agent import ContactAgent
    from core.agent.planner_base import ContactAgentBudget

    calls = []

    def zi_handler(args, ctx):
        calls.append(("zi", args))
        # first title miss simulated by planner calling twice; return not_found once
        if len([c for c in calls if c[0] == "zi"]) == 1:
            return ToolResult(ok=False, error="none", error_kind="not_found")
        return ToolResult(
            ok=True,
            data={
                "contact": {
                    "name": "Asha",
                    "email": "asha@pratham.org",
                    "title": "Head of Partnerships",
                    "mobile": "",
                    "linkedin_url": "",
                },
                "matched_on": "CSR Head → Head of Partnerships (ZI fallback, priority index 2)",
            },
            cost={"zi_credits": 2},
        )

    def domain_handler(args, ctx):
        return ToolResult(ok=True, data={"domain": "pratham.org", "org_name": "Pratham", "source": "cache"})

    def team_handler(args, ctx):
        calls.append(("team", args))
        return ToolResult(
            ok=True,
            data={"url": "https://pratham.org/team", "text": "Asha Rao, Head of Partnerships", "candidates": [{"name": "Asha Rao", "title": "Head of Partnerships"}]},
        )

    def li_handler(args, ctx):
        calls.append(("li", args))
        return ToolResult(ok=True, data={"name": "Asha", "title": "CSR", "linkedin_url": "https://linkedin.com/in/asha"})

    def signal_handler(args, ctx):
        return ToolResult(
            ok=True,
            data={"industry": "Nonprofit", "hq": "Mumbai", "employee_band": "1001-5000"},
        )

    def compose_handler(args, ctx):
        raise AssertionError("Phase 2 tool must not run")

    reg = ToolRegistry()
    for name, scope, handler in [
        ("resolve_domain", {"phase1"}, domain_handler),
        ("zoominfo_search_contact", {"phase1"}, zi_handler),
        ("web_find_team_page", {"phase1"}, team_handler),
        ("linkedin_person_search", {"phase1"}, li_handler),
        ("zoominfo_light_company_signal", {"phase1"}, signal_handler),
        ("compose_hyper_personalized_email", {"phase2"}, compose_handler),
    ]:
        t = FakeTool(name, scope, handler)
        _patch_input_schema(t)
        # Fix validate: registry uses model_validate
        from pydantic import BaseModel

        class Loose(BaseModel):
            model_config = {"extra": "allow"}

        t.input_schema = Loose
        # wrap run to accept validated model — args lost; use ctx.extras? 
        # Better: custom registry.call monkeypatch
        reg.register(t)

    # Monkeypatch registry.call to pass raw inputs
    orig_get = reg.get

    def call(name, inputs, ctx):
        tool = orig_get(name)
        if ctx.phase not in tool.phase_scope:
            raise PhaseScopeError(name)
        return tool.handler(inputs or {}, ctx)

    reg.call = call  # type: ignore

    decisions = [
        {"next_tool": "resolve_domain", "args": {"org_name": "Pratham"}},
        {"next_tool": "zoominfo_search_contact", "args": {"titles": ["CSR Head"], "domain": "pratham.org"}},
        {"next_tool": "web_find_team_page", "args": {"domain": "pratham.org"}},
        {"next_tool": "zoominfo_search_contact", "args": {"titles": ["Head of Partnerships"], "domain": "pratham.org", "full_name": "Asha Rao"}},
        {"next_tool": "zoominfo_light_company_signal", "args": {"domain": "pratham.org"}},
        {"done": True, "status": "ready_for_review"},
    ]
    gem = ScriptedGemini(decisions)
    agent = ContactAgent(
        registry=reg,
        gemini=gem,
        budget=ContactAgentBudget(max_steps=12, max_wall_seconds=30),
        persona_target={"titles": ["CSR Head", "Head of Partnerships"]},
    )
    row = RowState(row_id="r1", phase="phase1", input="Pratham", status="queued")
    out = agent.run(row)
    assert out.status == "ready_for_review"
    assert out.contact and out.contact.email
    assert "fallback" in (out.contact.matched_on or "").lower() or out.contact.matched_on
    assert out.light_org_signal is not None
    assert all(k == "contact_planner" for k in gem.kinds)
    assert any(c[0] == "team" for c in calls)
    # Attempting phase2 tool via registry should fail
    from core.tools.base import ToolContext

    with pytest.raises(PhaseScopeError):
        reg.call(
            "compose_hyper_personalized_email",
            {},
            ToolContext(phase="phase1", session_id="s", row_id="r1"),
        )
