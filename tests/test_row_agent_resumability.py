# NOTE: RowState persistence enables resume mid-loop.
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.agent.contact_agent import ContactAgent
from core.agent.planner_base import ContactAgentBudget, RowState
from core.tools.base import ToolResult
from core.tools.registry import ToolRegistry


class ScriptedGemini:
    def __init__(self, decisions):
        self.decisions = list(decisions)

    def generate(self, task_kind, prompt, **kwargs):
        d = self.decisions.pop(0)

        class R:
            parsed = d
            text = json.dumps(d)
            tokens_in = 1
            tokens_out = 1
            wall_ms = 1
            repaired = False

        return R()


def test_resume_from_persisted_row_state():
    persisted = {}

    def persist(row: RowState):
        persisted["row"] = row.model_dump(mode="json")

    reg = ToolRegistry()

    def call(name, inputs, ctx):
        if name == "resolve_domain":
            return ToolResult(ok=True, data={"domain": "a.org", "org_name": "A"})
        if name == "zoominfo_light_company_signal":
            return ToolResult(
                ok=True,
                data={"industry": "NGO", "hq": "Delhi", "employee_band": "100"},
            )
        if name == "zoominfo_search_contact":
            return ToolResult(
                ok=True,
                data={
                    "contact": {
                        "name": "N",
                        "email": "n@a.org",
                        "title": "CSR",
                        "mobile": "",
                        "linkedin_url": "",
                    },
                    "matched_on": "CSR",
                },
            )
        return ToolResult(ok=False, error="no", error_kind="not_found")

    reg.call = call  # type: ignore
    reg.manifest = lambda phase=None: [  # type: ignore
        {"name": "resolve_domain"},
        {"name": "zoominfo_search_contact"},
        {"name": "zoominfo_light_company_signal"},
    ]

    # First session: only resolve domain then "kill"
    gem1 = ScriptedGemini(
        [
            {"next_tool": "resolve_domain", "args": {"org_name": "A"}},
            {"done": True, "status": "paused_budget", "reason": "simulated kill"},
        ]
    )
    agent = ContactAgent(
        registry=reg,
        gemini=gem1,
        budget=ContactAgentBudget(max_steps=10),
        persist_row=persist,
    )
    row = RowState(row_id="r1", phase="phase1", input="A", status="queued")
    out1 = agent.run(row)
    assert out1.resolved_domain == "a.org"
    assert persisted["row"]["resolved_domain"] == "a.org"

    # Resume from blob
    row2 = RowState.model_validate(persisted["row"])
    gem2 = ScriptedGemini(
        [
            {
                "next_tool": "zoominfo_search_contact",
                "args": {"domain": "a.org", "titles": ["CSR"]},
            },
            {"next_tool": "zoominfo_light_company_signal", "args": {"domain": "a.org"}},
            {"done": True, "status": "ready_for_review"},
        ]
    )
    agent2 = ContactAgent(
        registry=reg,
        gemini=gem2,
        budget=ContactAgentBudget(max_steps=10),
        persist_row=persist,
    )
    out2 = agent2.run(row2)
    assert out2.status == "ready_for_review"
    assert out2.contact and out2.contact.email == "n@a.org"
    assert out2.light_org_signal is not None
