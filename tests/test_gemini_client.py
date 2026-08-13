# NOTE: GeminiClient params + JSON repair + model-id grep guard.
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class FakeLLM:
    def __init__(self):
        self.calls = []
        self.responses = []

    def generate_content_raw(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        if self.responses:
            return self.responses.pop(0)
        return {
            "text": json.dumps({"ok": True}),
            "tokens_in": 11,
            "tokens_out": 3,
        }


def test_task_kind_selects_params(monkeypatch):
    from core.agent import gemini_client as gc

    fake = FakeLLM()
    client = gc.GeminiClient(fake)
    monkeypatch.setattr(gc, "log_gemini_call", lambda *a, **k: None, raising=False)
    # avoid drive
    monkeypatch.setattr(client, "_log", lambda *a, **k: None)
    client.generate("grounding_check", "ping")
    assert fake.calls[0]["temperature"] == 0.0
    fake.calls.clear()
    client.generate("compose_email", "ping")
    assert fake.calls[0]["temperature"] == 0.7


def test_json_repair(monkeypatch):
    from core.agent import gemini_client as gc

    fake = FakeLLM()
    fake.responses = [
        {"text": "HERE IS JSON:\n{not valid", "tokens_in": 1, "tokens_out": 1},
        {"text": '{"fixed": true}', "tokens_in": 1, "tokens_out": 1},
    ]
    client = gc.GeminiClient(fake)
    monkeypatch.setattr(client, "_log", lambda *a, **k: None)
    # first parse fails → repair path uses second generate_content_raw
    # But generate() calls _call_with_retry once then _repair_json
    # So seed: first response malformed, repair returns good
    fake.responses = [
        {"text": "not json at all {{{", "tokens_in": 1, "tokens_out": 1},
    ]
    # repair calls generate_content_raw directly
    original = fake.generate_content_raw

    def side(prompt, **kwargs):
        fake.calls.append(1)
        if "Malformed" in prompt or "strict JSON" in prompt:
            return {"text": '{"done": true, "status": "ready"}', "tokens_in": 1, "tokens_out": 1}
        return {"text": "<<<not json>>>", "tokens_in": 1, "tokens_out": 1}

    fake.generate_content_raw = side
    resp = client.generate("contact_planner", "hi")
    assert resp.repaired is True
    assert resp.parsed["done"] is True


def test_no_hardcoded_model_ids_in_new_code():
    """Fail if agent/tools/bulk introduce hardcoded gemini model IDs.

    Existing config.py / render.yaml / .env.example pins are allowed (do not
    change underlying GEMINI_MODEL wiring).
    """
    pat = re.compile(r"gemini-(?:2\.5|3\.5|3\.6)[a-z0-9.\-]*", re.I)
    allow = {
        "config.py",
        "render.yaml",
        ".env.example",
        "secrets.toml.example",
        "README.md",
        "gemini_params.yaml",  # must not contain — checked separately
    }
    bad = []
    for path in ROOT.rglob("*"):
        if path.suffix not in {".py", ".yaml", ".yml"}:
            continue
        if any(p in path.parts for p in (".venv", "venv", ".git", "__pycache__", ".cache")):
            continue
        if path.name in allow or path.name.endswith(".example"):
            continue
        # allow config defaults only via allow list
        text = path.read_text(encoding="utf-8", errors="ignore")
        if pat.search(text):
            # gemini_params.yaml must never have model ids
            bad.append(str(path.relative_to(ROOT)))
    # gemini_params.yaml explicit
    yaml_text = (ROOT / "gemini_params.yaml").read_text(encoding="utf-8")
    assert not pat.search(yaml_text)
    # new modules must be clean
    for b in bad:
        assert not b.startswith("core/agent"), b
        assert not b.startswith("core/tools"), b
        assert b not in {"core/bulk_pipeline.py", "core/hyper_drafter.py", "core/drive_db.py"}
