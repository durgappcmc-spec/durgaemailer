# NOTE: Like-sent bulk drafts must use last-search contacts + each company name.
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.intent import (
    parse_explicit_draft_company,
    parse_like_sent_request,
    wants_previous_chat_recipient,
    wants_prospect_list_recipients,
)
from agent.router import (
    _infer_like_sent_target,
    _personalize_like_sent_job,
    _reference_org_aliases,
    _unique_prospect_companies,
)


def test_to_above_means_prospect_list():
    assert wants_prospect_list_recipients(
        "bulk draft like info@magicbusindia.org to above"
    )
    assert wants_prospect_list_recipients("draft emails to these contacts")
    assert wants_prospect_list_recipients("create drafts for the previous prospects")
    assert not wants_prospect_list_recipients(
        "draft like info@magicbusindia.org for Flipkart"
    )


def test_sterlite_like_indiamart_query():
    msg = (
        "use the previous email from chat and draft to sterlite tech "
        "like khurshidalam.qureshi@indiamart.com"
    )
    parsed = parse_like_sent_request(msg)
    assert parsed is not None
    assert parsed["reference"].lower() == "khurshidalam.qureshi@indiamart.com"
    assert "sterlite" in (parsed.get("target") or "").lower()
    assert parse_explicit_draft_company(msg).lower().startswith("sterlite")
    # "previous email from chat" is style — not prior Magic Bus To
    assert not wants_previous_chat_recipient(msg)
    assert wants_prospect_list_recipients(msg)


def test_infer_target_empty_for_multi_company_list():
    prospects = [
        {"company": "Sterlite Tech", "email": "a@st.com"},
        {"company": "Other Co", "email": "b@o.com"},
    ]
    assert (
        _infer_like_sent_target(
            explicit="",
            reference="info@magicbusindia.org",
            prospects=prospects,
            history=None,
            prefer_per_prospect=True,
        )
        == ""
    )


def test_unique_prospect_companies_ordered():
    rows = [
        {"company": "Sterlite Tech", "email": "a@s.com"},
        {"company": "Sterlite Tech", "email": "b@s.com"},
        {"company": "Other Co", "email": "c@o.com"},
    ]
    assert _unique_prospect_companies(rows) == ["Sterlite Tech", "Other Co"]


def test_personalize_scrubs_magic_bus_for_sterlite():
    scrub = _reference_org_aliases(
        "info@magicbusindia.org",
        {
            "to": '"Magic Bus India" <info@magicbusindia.org>',
            "subject": "Partnership with Magic Bus",
            "body_text": "Dear Magic Bus Team,\n\nWe love Magic Bus programs.\n",
            "body_html": "",
        },
        "magicbusindia",
    )
    assert any("magic bus" in s.lower() for s in scrub)
    out = _personalize_like_sent_job(
        subject="Partnership with Magic Bus",
        html_body="<p>Dear Magic Bus Team,</p><p>We love Magic Bus programs.</p>",
        prospect={
            "name": "Priya Sharma",
            "first_name": "Priya",
            "company": "Sterlite Tech",
            "email": "priya@sterlite.com",
        },
        scrub_names=scrub,
    )
    blob = (out["subject"] + out["html_body"]).lower()
    assert "sterlite" in blob
    assert "magic bus" not in blob
    assert "magicbus" not in blob.replace("sterlite", "")


def test_personalize_rewrites_khushid_greeting():
    out = _personalize_like_sent_job(
        subject="Partnership",
        html_body="<p>Hi Khushid,</p><p>We would love to partner.</p>",
        prospect={
            "name": "Priya Sharma",
            "first_name": "Priya",
            "title": "CSR Head",
            "company": "Sterlite Tech",
            "email": "priya@sterlite.com",
        },
        scrub_names=["IndiaMART", "indiamart"],
    )
    assert "Hi Priya," in out["html_body"]
    assert "Khushid" not in out["html_body"]
    assert "CSR Head" not in out["html_body"].split(",")[0]


def test_like_sent_clone_drops_click_tracking_autolink():
    from agent.router import _full_reference_text, _full_text_to_html

    text = (
        "Hi Khushid,\n\nSee our work "
        "<https://durgaemailer-tracking.netlify.app/.netlify/functions/"
        "click?id=94a2ee50-3415-4466-afed-a4c1e3cb3081>\n"
    )
    cleaned = _full_reference_text(text, "")
    assert "netlify" not in cleaned.lower()
    assert "click?id=" not in cleaned
    html = _full_text_to_html(cleaned)
    assert "netlify" not in html.lower()
    assert "click?id=" not in html
