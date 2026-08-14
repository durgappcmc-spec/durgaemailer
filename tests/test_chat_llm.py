# NOTE: Chat model preference + Genspark compose routing (no live APIs).
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.genspark_client import parse_json_loose


def test_parse_json_loose_fence_and_object():
    assert parse_json_loose('{"a": 1}')["a"] == 1
    assert parse_json_loose("```json\n{\"b\": 2}\n```")["b"] == 2


def test_chat_llm_save_and_reload(tmp_path, monkeypatch):
    import core.chat_llm as chat_llm

    monkeypatch.setattr(chat_llm, "_PREF_FILE", tmp_path / "chat_llm.json")
    monkeypatch.setattr(chat_llm, "_cached", None)
    monkeypatch.setattr(
        "core.durable_store.load_session_extras", lambda **_k: {}, raising=False
    )
    monkeypatch.setattr(
        "core.durable_store.save_json_blob_async", lambda *_a, **_k: True, raising=False
    )
    chat_llm.reset_cache()
    assert chat_llm.load_provider() == "gemini"
    chat_llm.save_provider("genspark")
    chat_llm.reset_cache()
    assert chat_llm.load_provider() == "genspark"
    chat_llm.save_provider("gemini")
    chat_llm.reset_cache()
    assert chat_llm.load_provider() == "gemini"


def test_compose_uses_saved_genspark_not_just_key(monkeypatch):
    from core import style_draft

    n = {"i": 0}

    monkeypatch.setattr("core.chat_llm.resolve_chat_provider", lambda: "genspark")
    monkeypatch.setattr("core.genspark_client.available", lambda: True)

    def fake_json(prompt, **kwargs):
        n["i"] += 1
        if "failed a requirement" in prompt:
            return {
                "subject": "Skilling partnership",
                "body": "Hi Jane,\n\nLet's discuss your skilling program.\n\nBest,",
            }
        return {"subject": "Hi", "body": "Hello there."}

    def fake_review(**kwargs):
        body = kwargs.get("body") or ""
        ok = "skilling" in body.lower()
        return {
            "ok": ok,
            "score": 0.9 if ok else 0.2,
            "issues": [] if ok else ["missing skilling ask"],
            "missing_requirements": [] if ok else ["skilling"],
        }

    monkeypatch.setattr("core.genspark_client.compose_json", fake_json)
    monkeypatch.setattr("core.genspark_client.review_email", fake_review)

    out = style_draft.compose_styled_email(
        to_email="jane@acme.com",
        enrichment={"first_name": "Jane", "company": "Acme"},
        user_msg="draft to jane@acme.com about their skilling program",
    )
    assert out["provider"] == "genspark"
    assert n["i"] == 2
    assert out["quality_ok"] is True
    assert "skilling" in (out["body_cleaned"] or "").lower()


def test_compose_stays_on_gemini_when_selected(monkeypatch):
    from core import style_draft

    called = {"gsk": False}

    monkeypatch.setattr("core.chat_llm.resolve_chat_provider", lambda: "gemini")
    monkeypatch.setattr("core.genspark_client.available", lambda: True)

    def boom(*_a, **_k):
        called["gsk"] = True
        raise AssertionError("Genspark should not run when Gemini is selected")

    monkeypatch.setattr("core.genspark_client.compose_json", boom)
    monkeypatch.setattr(
        "core.llm.extract_json",
        lambda *_a, **_k: json.dumps(
            {"subject": "Hello", "body": "Hi Jane,\n\nThanks.\n\nBest,"}
        ),
    )

    out = style_draft.compose_styled_email(
        to_email="jane@acme.com",
        enrichment={"first_name": "Jane"},
        user_msg="draft to jane@acme.com",
    )
    assert called["gsk"] is False
    assert out["provider"] == "gemini"
    assert "Hi Jane" in (out["body_cleaned"] or "")
