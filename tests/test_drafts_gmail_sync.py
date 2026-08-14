# NOTE: Drafts page Gmail round-trip helpers (no live Gmail).
from __future__ import annotations

import base64
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gmail_client.drafts import (
    _drop_addrs_already_in_to,
    _extract_plain_body,
    normalize_addr_list,
)
from gmail_client.html_format import render_draft_html


def test_normalize_addr_list_dedupes_case_and_semicolons():
    assert normalize_addr_list("a@x.com, a@x.com; A@X.com") == "a@x.com"
    assert normalize_addr_list("") == ""
    assert normalize_addr_list("b@y.com, a@x.com") == "b@y.com, a@x.com"


def test_drop_cc_already_in_to():
    out = _drop_addrs_already_in_to(
        "colleague@karunamedia.org, jane@acme.com",
        "Jane Doe <jane@acme.com>",
    )
    assert "jane@acme.com" not in out.lower()
    assert "colleague@karunamedia.org" in out.lower()


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


def test_extract_plain_prefers_text_plain_no_clean():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {
                "mimeType": "text/plain",
                "body": {"data": _b64("Hello  world.\nThis line stays.\n")},
            },
            {
                "mimeType": "text/html",
                "body": {"data": _b64("<p>Hello  world.</p><p>HTML side</p>")},
            },
        ],
    }
    body = _extract_plain_body(payload)
    assert "Hello  world." in body
    assert "This line stays." in body
    assert "HTML side" not in body


def test_extract_plain_falls_back_to_stripped_html():
    payload = {
        "mimeType": "text/html",
        "body": {"data": _b64("<p>Hi Jane,</p><p>Thanks &amp; more.</p>")},
    }
    body = _extract_plain_body(payload)
    assert "Hi Jane" in body
    assert "Thanks & more." in body
    assert "<p>" not in body


def test_render_draft_html_includes_cc_and_local_bcc():
    html = render_draft_html(
        "Hello",
        "jane@acme.com",
        "cc@x.com",
        "Hi Jane,\n\nThanks.",
        bcc="blind@x.com",
        bcc_local=True,
    )
    assert "jane@acme.com" in html
    assert "cc@x.com" in html
    assert "blind@x.com" in html
    assert "(local)" in html
