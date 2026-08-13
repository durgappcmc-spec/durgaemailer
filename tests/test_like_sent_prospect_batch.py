# NOTE: Like-sent bulk drafts must use last-search contacts + each company name.
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.intent import wants_prospect_list_recipients
from agent.router import (
    _infer_like_sent_target,
    _personalize_like_sent_job,
    _reference_org_aliases,
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
    from agent.router import _unique_prospect_companies

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
