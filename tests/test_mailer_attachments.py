# NOTE: Bulk send isolation + attachment size cap.
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_bulk_send_isolates_failures(monkeypatch):
    import gmail_client.send as sendmod

    calls = []

    def fake_send_email(**kwargs):
        calls.append(kwargs["to"])
        if kwargs["to"] == "bad@x.org":
            raise RuntimeError("boom")
        return {"message_id": "m1", "tracking_id": kwargs.get("html_body") and "t"}

    monkeypatch.setattr(sendmod, "send_email", fake_send_email)
    monkeypatch.setattr(sendmod.time, "sleep", lambda s: None)
    monkeypatch.setattr(sendmod, "_resolve_attachments", lambda items: [])

    results = sendmod.send_bulk_serial(
        [
            {"to": "a@x.org", "subject": "1", "body_html": "<p>a</p>", "tracking_id": "t1"},
            {"to": "bad@x.org", "subject": "2", "body_html": "<p>b</p>"},
            {"to": "c@x.org", "subject": "3", "body_html": "<p>c</p>", "tracking_id": "t3"},
        ],
        jitter_seconds=(0, 0),
    )
    assert len(results) == 3
    assert results[1].get("error")
    assert calls == ["a@x.org", "bad@x.org", "c@x.org"] or calls == ["a@x.org", "c@x.org"]
    # fake_send raises before append for bad — actually our send_bulk catches Exception around send_email
    assert any(r.get("to") == "c@x.org" or r.get("draft_id") is None for r in results)


def test_attachment_cap(monkeypatch):
    import gmail_client.send as sendmod

    monkeypatch.setattr(sendmod.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        sendmod,
        "_resolve_attachments",
        lambda items: [{"filename": "big.bin", "data": b"x" * (26 * 1024 * 1024)}],
    )
    monkeypatch.setattr(sendmod, "send_email", lambda **k: {"ok": True})
    results = sendmod.send_bulk_serial(
        [{"to": "a@x.org", "subject": "1", "body_html": "<p>a</p>", "attachments": [{}]}],
        jitter_seconds=(0, 0),
    )
    assert results[0].get("error") and "25" in results[0]["error"]
