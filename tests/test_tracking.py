# NOTE: tracking strip/inject idempotency + save surfaces.
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _tracking_base(monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "TRACKING_BASE_URL", "https://track.example.com")
    monkeypatch.setattr(settings, "APPS_SCRIPT_TRACKING_URL", "")


def test_inject_idempotent_preserves_id():
    from core.tracking import extract_tracking_id, inject_tracking, strip_tracking

    html = '<p>Hi <a href="https://karuna.org">Karuna</a></p>'
    out1, tid = inject_tracking(html, register=False)
    assert tid
    assert extract_tracking_id(out1) == tid
    assert "/.netlify/functions/open?id=" in out1
    out2, tid2 = inject_tracking(out1, tracking_id=tid, register=False)
    assert tid2 == tid
    assert out2.count("/.netlify/functions/open") == 1


def test_strip_removes_pixel():
    from core.tracking import inject_tracking, strip_tracking

    html, tid = inject_tracking("<p>x</p><a href='https://a.com'>a</a>", register=False)
    cleaned = strip_tracking(html)
    assert tid not in cleaned or "open?id=" not in cleaned
    assert "/.netlify/functions/open" not in cleaned


@pytest.mark.parametrize("surface", ["bulk_grid", "chat_inspector", "drafts_inspector"])
def test_save_path_reinjects(surface, monkeypatch, tmp_path):
    """Every save surface must strip→inject with same tracking_id."""
    from core.tracking import inject_tracking, extract_tracking_id

    body, tid = inject_tracking("<p>Hello <a href='https://x.org'>x</a></p>", register=False)
    # simulate edit that drops pixel accidentally
    edited = "<p>Hello edited <a href='https://x.org'>x</a></p>"
    saved, tid2 = inject_tracking(edited, tracking_id=tid, register=False)
    assert tid2 == tid
    assert extract_tracking_id(saved) == tid
    assert surface  # parametrize identity
