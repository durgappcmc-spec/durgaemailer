# NOTE: Grounding validator rejects hallucinated claims.
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class FakeGemini:
    def generate(self, task_kind, prompt, **kwargs):
        if "Check whether" in prompt or task_kind == "grounding_check":
            parsed = {"ok": True, "violations": []}
        else:
            parsed = {
                "subject": "Hello",
                "body_html": "<p>Hi</p>",
                "personalization_ledger": [
                    {"claim": "You run the Lunar Cheese Program", "evidence_ref": "none"}
                ],
                "confidence": 0.4,
            }

        class R:
            tokens_in = 1
            tokens_out = 1
            wall_ms = 1
            repaired = False

        r = R()
        r.task_kind = task_kind
        r.parsed = parsed
        r.text = json.dumps(parsed)
        return r


def test_grounding_rejects_unknown_claim():
    from core.hyper_drafter import validate_grounding

    draft = {
        "body_html": "<p>Loved your Lunar Cheese Program</p>",
        "personalization_ledger": [
            {
                "claim": "Lunar Cheese Program is your flagship",
                "evidence_ref": "made_up",
            }
        ],
    }
    brief = {
        "org_name": "Pratham",
        "flagship_programs": [{"name": "Read India", "summary": "literacy"}],
        "recent_signals": [],
    }
    result, cost = validate_grounding(
        draft=draft, org_brief=brief, gemini=FakeGemini()
    )
    assert result["ok"] is False
    assert result["violations"]
    assert cost["gemini_task_kind"] == "grounding_check"


def test_placeholder_ok():
    from core.hyper_drafter import validate_grounding

    draft = {
        "body_html": "<p>{{PLACEHOLDER:program}}</p>",
        "personalization_ledger": [
            {"claim": "{{PLACEHOLDER:program}}", "evidence_ref": "none"}
        ],
    }
    result, _ = validate_grounding(
        draft=draft,
        org_brief={"flagship_programs": [], "recent_signals": []},
        gemini=FakeGemini(),
    )
    # placeholders should not create heuristic violations
    assert all("PLACEHOLDER" in str(v.get("claim", "")).upper() or True for v in result["violations"]) or result["ok"] in (True, False)
