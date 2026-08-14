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


def test_collect_draft_refs_paginates():
    from gmail_client.drafts import _collect_draft_refs

    pages = {
        None: {
            "drafts": [
                {"id": "d1", "message": {"id": "m1"}},
                {"id": "d2", "message": {"id": "m2"}},
            ],
            "nextPageToken": "p2",
        },
        "p2": {"drafts": [{"id": "d3", "message": {"id": "m3"}}]},
    }

    def lister(token):
        return pages[token]

    refs = _collect_draft_refs(lister, limit=10)
    assert refs == [("d1", "m1"), ("d2", "m2"), ("d3", "m3")]


def test_collect_draft_folder_message_ids_paginates():
    from gmail_client.drafts import _collect_draft_folder_message_ids

    pages = {
        None: {
            "messages": [{"id": "m1"}, {"id": "m2"}],
            "nextPageToken": "n2",
        },
        "n2": {"messages": [{"id": "m3"}]},
    }

    ids = _collect_draft_folder_message_ids(lambda t: pages[t], limit=10)
    assert ids == ["m1", "m2", "m3"]
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


def test_extract_html_prefers_text_html():
    from gmail_client.drafts import _extract_html_body

    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {
                "mimeType": "text/plain",
                "body": {"data": _b64("Hello plain\n\nSecond")},
            },
            {
                "mimeType": "text/html",
                "body": {
                    "data": _b64(
                        "<p>Hello</p><ul><li>One</li></ul>"
                        '<div class="gmail_signature">Best</div>'
                    )
                },
            },
        ],
    }
    html = _extract_html_body(payload)
    assert "<ul>" in html
    assert "<li>One</li>" in html
    assert "gmail_signature" in html
    assert "Hello plain" not in html


def test_extract_html_accepts_mime_charset_and_unwraps_document():
    from gmail_client.drafts import _extract_html_body
    from gmail_client.html_format import html_for_editor

    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {
                "mimeType": "text/html; charset=utf-8",
                "body": {
                    "data": _b64(
                        "<html><body><div dir='ltr'><p>Hi Jane,</p>"
                        "<ul><li>One</li></ul></div></body></html>"
                    )
                },
            }
        ],
    }
    html = _extract_html_body(payload)
    assert "Hi Jane" in html
    assert "<ul>" in html
    inner = html_for_editor(html)
    assert "<html>" not in inner.lower()
    assert "<body>" not in inner.lower()
    assert "Hi Jane" in inner


def test_extract_html_falls_back_to_wrapped_plain():
    from gmail_client.drafts import _extract_html_body

    payload = {
        "mimeType": "text/plain",
        "body": {"data": _b64("Hi Jane,\n\nThanks.")},
    }
    html = _extract_html_body(payload)
    assert "<p>" in html
    assert "Hi Jane," in html
    assert "Thanks." in html


def test_sanitize_strips_script_iframe_and_on_handlers():
    from gmail_client.html_format import sanitize_email_html

    out = sanitize_email_html(
        '<p>Hi</p><script>alert(1)</script><iframe src="x"></iframe>'
        '<p onclick="evil()">ok</p>'
    )
    low = out.lower()
    assert "<script" not in low
    assert "<iframe" not in low
    assert "onclick" not in low
    assert "ok" in out


def test_render_gmail_preview_keeps_raw_lists_and_signature():
    from gmail_client.html_format import render_gmail_preview

    body = (
        "<p>Hi</p><ul><li>One</li></ul>"
        '<div class="gmail_signature" data-smartmail="gmail_signature">'
        "<p>Best,<br>Durga</p></div>"
    )
    html = render_gmail_preview("Hello", "jane@acme.com", "cc@x.com", body)
    assert "<ul>" in html
    assert "<li>One</li>" in html
    assert "gmail_signature" in html
    assert "&lt;ul&gt;" not in html
    assert "gm-preview" in html
    assert "jane@acme.com" in html


def test_mime_from_content_type_header():
    from gmail_client.drafts import _extract_html_body, _mime_base

    part = {
        "headers": [{"name": "Content-Type", "value": "text/html; charset=UTF-8"}],
        "body": {"data": _b64("<p>Hi Jane</p>")},
    }
    assert _mime_base(part) == "text/html"
    html = _extract_html_body(part)
    assert "Hi Jane" in html


def test_extract_html_ignores_empty_wrapper_uses_plain():
    from gmail_client.drafts import _extract_html_body

    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {
                "mimeType": "text/plain",
                "body": {"data": _b64("Hello Jane,\n\nThis is the real body.")},
            },
            {
                "mimeType": "text/html",
                "body": {"data": _b64('<div dir="ltr"><br></div>')},
            },
        ],
    }
    html = _extract_html_body(payload)
    assert "real body" in html
    assert "Hello Jane" in html


def test_extract_from_raw_multipart():
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    from gmail_client.drafts import _extract_from_raw

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Hi"
    msg.attach(MIMEText("plain body here", "plain"))
    msg.attach(MIMEText("<p>html body here</p>", "html"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii").rstrip("=")
    html, text = _extract_from_raw(raw)
    assert "html body" in html
    assert "plain body" in text


def test_html_for_editor_unwraps_document_and_style():
    from gmail_client.html_format import html_for_editor

    out = html_for_editor(
        "<html><head><style>p{color:red}</style></head>"
        "<body><div dir='ltr'><p>Hi Jane</p></div></body></html>"
    )
    assert "Hi Jane" in out
    assert "<html" not in out.lower()
    assert "<style" not in out.lower()


def test_with_signature_appends_once_and_replace_can_remove():
    from core.signatures import replace_signature, with_signature

    body = "<p>Hi Jane,</p><p>Thanks.</p>"
    sig = "<p>Best,<br>Durga</p>"
    once = with_signature(body, sig)
    assert 'class="gmail_signature"' in once
    twice = with_signature(once, sig)
    assert twice.count('class="gmail_signature"') == 1
    swapped = replace_signature(once, "<p>Short</p>")
    assert "Short" in swapped
    assert "Hi Jane" in swapped
    removed = replace_signature(swapped, "")
    assert "gmail_signature" not in removed
    assert "Hi Jane" in removed
