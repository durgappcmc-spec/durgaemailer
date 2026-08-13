# NOTE: Designation injection into draft greetings.
from __future__ import annotations

from agent.router import _apply_template, _ensure_designation_in_greeting


def test_ensure_designation_in_greeting():
    html = "<p>Hi Priya,</p><p>Hope you are well.</p>"
    out = _ensure_designation_in_greeting(
        html, first_name="Priya", title="CSR Head"
    )
    assert "Hi Priya (CSR Head)" in out


def test_apply_template_name_with_title():
    out = _apply_template(
        "Hi {name_with_title}, at {company}",
        {
            "first_name": "Priya",
            "title": "CSR Head",
            "company": "Sterlite Tech",
        },
    )
    assert out == "Hi Priya (CSR Head), at Sterlite Tech"


def test_designation_alias():
    out = _apply_template(
        "Role: {designation}",
        {"designation": "Head of CSR"},
    )
    assert out == "Role: Head of CSR"
