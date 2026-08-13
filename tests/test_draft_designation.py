# NOTE: Draft greetings use first name only — never "Name (Title)".
from __future__ import annotations

from agent.router import _apply_template, _ensure_designation_in_greeting


def test_ensure_designation_in_greeting():
    html = "<p>Hi Priya,</p><p>Hope you are well.</p>"
    out = _ensure_designation_in_greeting(
        html, first_name="Priya", title="CSR Head"
    )
    assert "Hi Priya," in out
    assert "(" not in out.split(",")[0]


def test_greeting_replaces_cloned_khushid():
    html = "<p>Hi Khushid,</p><p>Hope you are well.</p>"
    out = _ensure_designation_in_greeting(
        html, first_name="Priya", title="CSR Head"
    )
    assert "Hi Priya," in out
    assert "Khushid" not in out
    assert "CSR Head" not in out.split("<p>")[1]


def test_greeting_rewrites_name_without_title():
    html = "<p>Dear Khushid,</p><p>Quick note.</p>"
    out = _ensure_designation_in_greeting(html, first_name="Anupam", title="")
    assert "Dear Anupam," in out
    assert "Khushid" not in out


def test_greeting_strips_title_in_parentheses():
    html = "<p>Hi Sushmita (ESG Associate),</p><p>Hope you are well.</p>"
    out = _ensure_designation_in_greeting(
        html, first_name="Sushmita", title="ESG Associate"
    )
    assert "Hi Sushmita," in out
    assert "ESG Associate" not in out.split(",")[0]
    assert "(" not in out.split(",")[0]


def test_apply_template_name_with_title_is_first_name_only():
    out = _apply_template(
        "Hi {name_with_title}, at {company}",
        {
            "first_name": "Priya",
            "title": "CSR Head",
            "company": "Sterlite Tech",
        },
    )
    assert out == "Hi Priya, at Sterlite Tech"


def test_designation_alias():
    out = _apply_template(
        "Role: {designation}",
        {"designation": "Head of CSR"},
    )
    assert out == "Role: Head of CSR"
