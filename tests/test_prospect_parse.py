# NOTE: Unit tests for recovering prospects from Chat agent text.
from core.prospect_parse import parse_prospects_from_agent_text


def test_parse_numbered_blocks():
    text = """
Found **2** contacts (**2** with email) via zoominfo.

1. Name: Priya Shah
Title: VP Sales
Company: Sterlite Technologies
Email: priya.shah@sterlite.com
Phone:
Mobile: +91 99999
LinkedIn: https://linkedin.com/in/priya
Location:
Seniority:
Department:
Source: zoominfo

2. Name: Rohan Mehta
Title: Director
Company: Sterlite Tech
Email: rohan@stl.tech
"""
    rows = parse_prospects_from_agent_text(text, default_company="sterlite tech")
    assert len(rows) == 2
    assert rows[0]["email"] == "priya.shah@sterlite.com"
    assert rows[0]["company"] == "Sterlite Technologies"
    assert rows[1]["email"] == "rohan@stl.tech"


def test_parse_skips_placeholder_emails():
    text = "Contact a@b.com and real.person@sterlitetech.com for outreach."
    rows = parse_prospects_from_agent_text(text, default_company="Sterlite")
    assert len(rows) == 1
    assert rows[0]["email"] == "real.person@sterlitetech.com"
    assert rows[0]["company"] == "Sterlite"
