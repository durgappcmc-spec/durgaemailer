"""Markdown → HTML and signature dedupe helpers."""

from gmail_client.html_format import (
    apply_inline_markdown,
    body_looks_signed,
    normalize_email_html,
    plain_or_markdown_to_html,
    strip_trailing_signature_block,
)


def test_bold_and_italic_inline():
    out = apply_inline_markdown("Hello **world** and *italics*")
    assert "<strong>world</strong>" in out
    assert "<em>italics</em>" in out
    assert "**" not in out
    assert "*italics*" not in out


def test_plain_markdown_to_html_lists_and_bold():
    text = (
        "Hi there,\n\n"
        "Why **Sterlite** specifically:\n\n"
        "- Point one\n"
        "- Point **two**\n\n"
        "Thanks,"
    )
    html = plain_or_markdown_to_html(text)
    assert "<strong>Sterlite</strong>" in html
    assert "<ul>" in html
    assert "<li>" in html
    assert "**" not in html


def test_normalize_renders_markdown_inside_html():
    raw = "<p>See our **AI-integrated** crafts.</p><p>*Next step*: call.</p>"
    out = normalize_email_html(raw)
    assert "<strong>AI-integrated</strong>" in out
    assert "<em>Next step</em>" in out
    assert "**" not in out
    assert "*Next step*" not in out


def test_star_wrapped_text_becomes_italic():
    """*text* markdown must not stay visible in drafts."""
    out = apply_inline_markdown("Please review *text* today")
    assert "<em>text</em>" in out
    assert "*text*" not in out

    html = normalize_email_html(
        "<p>*The opportunity*</p><p>We can support *girls' skilling* at scale.</p>"
    )
    assert "<em>The opportunity</em>" in html
    assert "<em>girls' skilling</em>" in html
    assert "*The opportunity*" not in html
    assert "*girls' skilling*" not in html


def test_star_wrapped_heading_becomes_strong():
    html = plain_or_markdown_to_html("*The opportunity*")
    assert "<strong>The opportunity</strong>" in html
    assert "*The opportunity*" not in html


def test_star_list_markers_are_not_eaten_as_italic():
    html = plain_or_markdown_to_html("* Point one\n* Point two")
    assert "<li>" in html
    assert "Point one" in html
    assert "<em>Point one" not in html


def test_body_looks_signed_requires_real_sig():
    closing_only = "<p>Hello</p><p>Warm regards,</p><p>Alex</p>"
    assert body_looks_signed(closing_only) is False

    with_org = (
        "<p>Hello</p><p>Warm regards,</p>"
        "<p>Alex<br>KarunaMedia<br>csr@karunamedia.com</p>"
    )
    assert body_looks_signed(with_org) is True


def test_strip_trailing_signature_keeps_salutation():
    html = (
        "<p>Hi Anupam,</p>"
        "<p>Great to connect about **CSR**.</p>"
        "<p>Warm regards,</p>"
        "<p>Alex<br>KarunaMedia<br>+91 99999 99999<br>"
        "csr@karunamedia.com</p>"
    )
    out = strip_trailing_signature_block(html)
    assert "Warm regards" in out
    assert "karunamedia" not in out.lower()
    assert "99999" not in out
    # Markdown in remaining body should still be normalizable
    out2 = normalize_email_html(out)
    assert "<strong>CSR</strong>" in out2
