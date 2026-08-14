# NOTE: Genspark compose + rollback helpers (no live API).
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.draft_rollback import can_rollback, remember_created, rollback_last
from core.genspark_client import parse_json_loose


def test_parse_json_loose_fence_and_object():
    assert parse_json_loose('{"a": 1}')["a"] == 1
    assert parse_json_loose("```json\n{\"b\": 2}\n```")["b"] == 2


def test_rollback_remembers_and_deletes(monkeypatch):
    sess: dict = {}
    remember_created(sess, ["gmail:abc", "def"])
    assert can_rollback(sess)
    deleted: list[str] = []

    def _del(did):
        deleted.append(did)
        return {"ok": True}

    monkeypatch.setattr("gmail_client.drafts.delete_gmail_draft", _del)
    out = rollback_last(sess)
    assert out.get("ok") is True
    assert deleted == ["abc", "def"]
    assert can_rollback(sess) is False


def test_compose_styled_uses_genspark_and_retries(monkeypatch):
    from core import style_draft

    n = {"i": 0}

    def fake_available():
        return True

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

    monkeypatch.setattr("core.genspark_client.available", fake_available)
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
    assert out["subject"]
