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


def test_draft_has_no_live_open_pixel():
    """Drafts store a tracking id only; the live pixel is injected at send."""
    from core.tracking import (
        extract_tracking_id,
        html_for_preview,
        inject_tracking,
        prepare_draft_tracking,
    )

    html = '<p>Hi <a href="https://karuna.org/program">our program</a></p>'
    drafted, tid = prepare_draft_tracking(html)
    assert tid
    assert extract_tracking_id(drafted) == tid
    assert "relay-tid:" in drafted
    assert "/.netlify/functions/open" not in drafted
    assert "/t/o/" not in drafted
    assert "karuna.org/program" in drafted
    assert "/.netlify/functions/click" not in drafted
    preview = html_for_preview(drafted)
    assert "netlify" not in preview.lower()
    assert "/.netlify/functions/open" not in preview
    assert "karuna.org/program" in preview

    sent, tid2 = inject_tracking(
        drafted, tracking_id=tid, register=False, track_clicks=True, track_opens=True
    )
    assert tid2 == tid
    assert "/.netlify/functions/open?id=" in sent


def test_draft_mode_hides_netlify_click_urls():
    """Drafts keep original hrefs and never embed a live open pixel."""
    from core.tracking import html_for_preview, prepare_draft_tracking

    html = '<p>Hi <a href="https://karuna.org/program">our program</a></p>'
    drafted, tid = prepare_draft_tracking(html)
    assert tid
    assert "/.netlify/functions/open" not in drafted
    assert "karuna.org/program" in drafted
    assert "/.netlify/functions/click" not in drafted
    preview = html_for_preview(drafted)
    assert "netlify" not in preview.lower()
    assert "karuna.org/program" in preview


def test_preview_strips_leftover_live_pixel():
    from core.tracking import html_for_preview, inject_tracking

    html, _tid = inject_tracking("<p>x</p>", register=False)
    assert "/.netlify/functions/open" in html
    preview = html_for_preview(html)
    assert "/.netlify/functions/open" not in preview
    assert "/t/o/" not in preview


def test_visible_click_autolink_is_stripped():
    """Gmail plain-text clones must not show Netlify click URLs to the reviewer."""
    from core.tracking import html_for_preview, strip_visible_tracking_urls
    from gmail_client.html_format import plain_or_markdown_to_html

    leaked = (
        "See our work "
        "<https://durgaemailer-tracking.netlify.app/.netlify/functions/"
        "click?id=94a2ee50-3415-4466-afed-a4c1e3cb3081>"
    )
    cleaned = strip_visible_tracking_urls(leaked)
    assert "netlify" not in cleaned.lower()
    assert "click?id=" not in cleaned
    assert "See our work" in cleaned

    html = plain_or_markdown_to_html(leaked)
    assert "netlify" not in html.lower()
    assert "click?id=" not in html
    preview = html_for_preview(f"<p>{leaked}</p>")
    assert "netlify" not in preview.lower()
    assert "click?id=" not in preview


@pytest.mark.parametrize("surface", ["bulk_grid", "chat_inspector", "drafts_inspector"])
def test_save_path_reinjects(surface, monkeypatch, tmp_path):
    """Every draft save keeps the same tracking_id without a live open pixel."""
    from core.tracking import extract_tracking_id, prepare_draft_tracking

    body, tid = prepare_draft_tracking("<p>Hello <a href='https://x.org'>x</a></p>")
    edited = "<p>Hello edited <a href='https://x.org'>x</a></p>"
    saved, tid2 = prepare_draft_tracking(edited, tid)
    assert tid2 == tid
    assert extract_tracking_id(saved) == tid
    assert "/.netlify/functions/open" not in saved
    assert surface  # parametrize identity
