# NOTE: Tool contract + phase_scope enforcement.
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_registry_manifest_and_phase_scope():
    from core.tools import build_registry
    from core.tools.base import ToolContext
    from core.tools.registry import PhaseScopeError

    reg = build_registry()
    m1 = {t["name"] for t in reg.manifest("phase1")}
    m2 = {t["name"] for t in reg.manifest("phase2")}
    assert "resolve_domain" in m1
    assert "compose_hyper_personalized_email" not in m1
    assert "compose_hyper_personalized_email" in m2
    assert "ask_human" in m1 and "ask_human" in m2

    ctx = ToolContext(phase="phase1", session_id="s", row_id="r")
    with pytest.raises(PhaseScopeError):
        reg.call("compose_hyper_personalized_email", {}, ctx)


def test_every_tool_has_contract_fields():
    from core.tools import build_registry

    reg = build_registry()
    for t in reg.manifest():
        tool = reg.get(t["name"])
        assert tool.name
        assert tool.description
        assert issubclass(tool.input_schema, BaseModel)
        assert isinstance(tool.phase_scope, set)
        assert tool.phase_scope
