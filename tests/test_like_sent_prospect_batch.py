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


def test_personalize_email_only_recipient_does_not_crash():
    from agent.router import (
        _first_name_from_recipient,
        _like_sent_prospect_ctx,
        _personalize_like_sent_job,
    )

    job = {"recipient_email": "annie.mathews@savethechildren.in"}
    ctx = _like_sent_prospect_ctx(job, by_email={}, target_company="")
    assert ctx["first_name"]
    assert ctx["first_name"] == _first_name_from_recipient(email=job["recipient_email"])
    out = _personalize_like_sent_job(
        subject="Partnership",
        html_body="<p>Dear Sheetal,</p><p>Greetings from Puppets.</p>",
        prospect=ctx,
        scrub_names=[],
    )
    assert "Dear Annie" in out["html_body"] or "Hi Annie" in out["html_body"]
    assert "Sheetal" not in out["html_body"]

    empty = {"recipient_email": "a_ansari@savethechildren.in", "recipient_name": ""}
    ctx2 = _like_sent_prospect_ctx(empty, by_email={})
    assert ctx2["first_name"]
    _personalize_like_sent_job(
        subject="Hi",
        html_body="<p>Dear Sheetal,</p>",
        prospect=ctx2,
        scrub_names=["savethechildren.in"],
    )

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


def test_like_sent_clone_keeps_full_body_and_youtube():
    from agent.router import _compose_like_sent_email

    yt = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    html = f"""
    <p>Dear IndiaMART Team,</p>
    <p>We would like to explore a CSR partnership around education and media.</p>
    <p>Our work includes classroom films, teacher training, and community screenings.</p>
    <p>Watch a short film here: <a href="{yt}">Karuna classroom film</a></p>
    <p>We can also share a one-pager and a sample episode.</p>
    <p>Why IndiaMART specifically: your MSME network reaches teachers nationwide.</p>
    <p>Next step: a 20-minute call next week.</p>
    <p>Thank you,</p>
    """
    out = _compose_like_sent_email(
        user_msg="create an email like the one sent to IndiaMART for Flipkart",
        reference_msg={
            "subject": "CSR idea for IndiaMART",
            "body_text": "short gmail snippet only",
            "body_html": html,
        },
        reference_company="IndiaMART",
        target_company="Flipkart",
        research_notes="",
        first_name="Priya",
    )
    body = out["html_body"]
    assert yt in body
    assert "youtube.com/watch" in body
    assert "Flipkart" in (out["subject"] or "")
    assert "IndiaMART" not in (out["subject"] or "")
    assert "classroom films" in body.lower() or "teacher training" in body.lower()
    assert "one-pager" in body.lower() or "sample episode" in body.lower()
    assert _plain_word_count(body) > 40


def test_html_company_swap_does_not_rewrite_youtube_href():
    from agent.router import _replace_html_company_names

    yt = "https://youtu.be/abc123IndiaMART"
    html = f'<p>Hello IndiaMART</p><p><a href="{yt}">video</a></p>'
    out = _replace_html_company_names(html, "IndiaMART", "Flipkart")
    assert yt in out
    assert "Hello Flipkart" in out
    assert 'href="https://youtu.be/abc123IndiaMART"' in out


def test_full_reference_text_keeps_youtube_href():
    from agent.router import _full_reference_text

    yt = "https://www.youtube.com/watch?v=keepme"
    html = f'<p>See this film</p><p><a href="{yt}">Watch</a></p>'
    text = _full_reference_text("See this film", html)
    assert yt in text


def _plain_word_count(html: str) -> int:
    from bs4 import BeautifulSoup

    return len(BeautifulSoup(html, "html.parser").get_text(" ", strip=True).split())
