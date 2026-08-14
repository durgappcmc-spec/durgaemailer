# NOTE: Gmail signature pref + once-only append (no live Gmail).
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_signature_mode_persists(tmp_path, monkeypatch):
    import core.mail_prefs as prefs

    monkeypatch.setattr(prefs, "_PREF_FILE", tmp_path / "mail_prefs.json")
    monkeypatch.setattr(
        "core.durable_store.load_session_extras", lambda **_k: {}, raising=False
    )
    monkeypatch.setattr(
        "core.durable_store.save_json_blob_async", lambda *_a, **_k: True, raising=False
    )
    prefs.reset_cache()
    assert prefs.signature_mode() == "gmail"
    assert prefs.include_gmail_signature() is True
    prefs.save_signature_mode("none")
    prefs.reset_cache()
    assert prefs.signature_mode() == "none"
    assert prefs.include_gmail_signature() is False
    prefs.save_signature_mode("gmail")
    prefs.reset_cache()
    assert prefs.include_gmail_signature() is True


def test_append_gmail_signature_once_not_stacked(monkeypatch):
    from gmail_client.send import append_signature

    monkeypatch.setattr("core.mail_prefs.include_gmail_signature", lambda: True)
    monkeypatch.setattr(
        "gmail_client.send.get_signature",
        lambda *_a, **_k: "<p>Best,<br>Durga<br>Karuna Media</p>",
    )

    once = append_signature("<p>Hi Jane,</p><p>Thanks.</p>")
    assert once.count('class="gmail_signature"') == 1
    assert "Karuna Media" in once
    twice = append_signature(once)
    assert twice.count('class="gmail_signature"') == 1


def test_append_signature_none_strips_block(monkeypatch):
    from gmail_client.send import append_signature

    monkeypatch.setattr("core.mail_prefs.include_gmail_signature", lambda: False)
    monkeypatch.setattr(
        "gmail_client.send.get_signature",
        lambda *_a, **_k: "<p>Should not appear</p>",
    )
    body = (
        "<p>Hi Jane,</p>"
        '<div class="gmail_signature" data-smartmail="gmail_signature">'
        "<p>Old sig</p></div>"
    )
    out = append_signature(body)
    assert "gmail_signature" not in out
    assert "Should not appear" not in out
    assert "Hi Jane" in out
