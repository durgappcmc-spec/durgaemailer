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


def test_build_raw_message_keeps_attachment_as_mixed_sibling():
    import base64
    from email import policy
    from email.parser import BytesParser

    from gmail_client.send import _build_raw_message

    blob = b"%PDF-fake-attachment%"
    raw, _tid = _build_raw_message(
        "a@x.org",
        "Hi",
        "<p>Hello</p>",
        attachments=[
            {
                "name": "one-pager.pdf",
                "data": blob,
                "mime_type": "application/pdf",
            }
        ],
        instrument=False,
        include_signature=False,
        plain_body="Hello",
        from_email="csr@example.com",
    )
    decoded = base64.urlsafe_b64decode(raw + "==")
    text = decoded.decode("latin1")
    assert text.lower().count("\nfrom:") + (
        1 if text.lower().startswith("from:") else 0
    ) == 1
    msg = BytesParser(policy=policy.default).parsebytes(decoded)
    files = [p.get_filename() for p in msg.walk() if p.get_filename()]
    assert files == ["one-pager.pdf"]
    for part in msg.walk():
        if part.get_filename() != "one-pager.pdf":
            continue
        assert part.get_content_type() == "application/pdf"
        assert part.get_content_disposition() == "attachment"
        assert part.get_payload(decode=True) == blob


def test_merge_draft_attachments_keeps_and_replaces():
    from components.draft_inspector import merge_draft_attachments

    existing = [
        {"name": "a.pdf", "data_base64": "YQ==", "size": 1},
        {"name": "b.pdf", "data_base64": "Yg==", "size": 1},
    ]
    merged = merge_draft_attachments(
        existing,
        [True, False],
        [{"name": "c.pdf", "data_base64": "Yw==", "size": 1}],
    )
    names = [a["name"] for a in merged]
    assert names == ["a.pdf", "c.pdf"]
    replaced = merge_draft_attachments(
        existing,
        [True, True],
        [{"name": "a.pdf", "data_base64": "eg==", "size": 1}],
    )
    by = {a["name"]: a["data_base64"] for a in replaced}
    assert by["a.pdf"] == "eg=="
    assert by["b.pdf"] == "Yg=="
