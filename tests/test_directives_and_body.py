# NOTE: Directive parsing, body cleaning, and LinkedIn URL detection.
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.intent import _heuristic_plan, parse_directives
from connectors.zoominfo import extract_linkedin_url, extract_linkedin_urls
from core.enrich_cache import (
    format_enrichment_fields,
    format_enrichment_panel,
    get_cached_enrichment,
    normalize_linkedin_url,
    put_cached_enrichment,
)
from gmail_client.html_format import (
    clean_email_body,
    extract_style_structure,
    html_from_cleaned_body,
    render_draft_html,
)


def test_parse_directives_draft_to_only():
    d = parse_directives(
        "https://linkedin.com/in/janedoe draft to jane@acme.com"
    )
    assert d["to"].lower() == "jane@acme.com"
    assert d["template_from"] == ""
    assert any("janedoe" in u for u in d["linkedin_urls"])


def test_parse_directives_style_from_other_recipient():
    d = parse_directives(
        "draft to bob@acme.com like the one sent to alice@acme.com"
    )
    assert d["to"].lower() == "bob@acme.com"
    assert d["template_from"].lower() == "alice@acme.com"
    assert d["to"].lower() != d["template_from"].lower()


def test_parse_directives_combined_linkedin_and_style():
    d = parse_directives(
        "https://linkedin.com/in/janedoe draft to jane@acme.com like the "
        "one sent to alice@acme.com"
    )
    assert d["to"].lower() == "jane@acme.com"
    assert d["template_from"].lower() == "alice@acme.com"
    assert d["linkedin_urls"]


def test_parse_directives_same_style_cc_attach():
    d = parse_directives(
        "draft to jane@acme.com same style as sent to alice@acme.com "
        "cc mark@acme.com attach one-pager.pdf"
    )
    assert d["to"].lower() == "jane@acme.com"
    assert d["template_from"].lower() == "alice@acme.com"
    assert "mark@acme.com" in [e.lower() for e in d["cc"]]
    assert d["attachments"] and "one-pager.pdf" in d["attachments"][0].lower()


def test_parse_directives_warns_when_to_equals_template():
    d = parse_directives(
        "draft to alice@acme.com like the one sent to alice@acme.com"
    )
    assert d.get("same_to_and_template_warning") is True


def test_heuristic_linkedin_only_enriches_no_draft():
    plan = _heuristic_plan("https://www.linkedin.com/in/janedoe")
    assert plan.action == "prospect_enrich"
    assert plan.draft is False


def test_heuristic_linkedin_plus_draft_to():
    plan = _heuristic_plan(
        "https://www.linkedin.com/in/janedoe draft to jane@acme.com"
    )
    assert plan.action == "prospect_enrich"
    assert plan.draft is True
    assert plan.to_emails and plan.to_emails[0].lower() == "jane@acme.com"


def test_heuristic_bob_not_alice():
    plan = _heuristic_plan(
        "draft to bob@acme.com like the one sent to alice@acme.com"
    )
    assert plan.action == "draft_email"
    assert plan.to_emails == ["bob@acme.com"]
    assert plan.like_sent_to.lower() == "alice@acme.com"
    assert "alice@acme.com" not in [e.lower() for e in plan.to_emails]


def test_extract_company_linkedin_url():
    urls = extract_linkedin_urls(
        "see https://www.linkedin.com/company/acme-corp/ and also "
        "https://linkedin.com/in/janedoe"
    )
    kinds = " ".join(urls)
    assert "/company/acme-corp" in kinds
    assert "/in/janedoe" in kinds
    assert extract_linkedin_url("https://linkedin.com/in/janedoe").endswith(
        "/janedoe"
    )


def test_clean_email_body_unwraps_and_collapses_spaces():
    raw = "Hello  world.\nThis continues  the sentence.\n\n\nNext para.  \n"
    out = clean_email_body(raw)
    assert "  " not in out
    assert "Hello world. This continues the sentence." in out
    assert out.count("\n\n") == 1
    assert out.endswith("\n")
    assert not out.endswith(" \n")


def test_html_from_cleaned_and_render_preview():
    cleaned = clean_email_body("Hi Jane,\n\nThanks for your time.\n\nBest regards,")
    html = html_from_cleaned_body(cleaned)
    assert "<p>" in html
    assert "**" not in html
    preview = render_draft_html("Hello", "jane@acme.com", "", cleaned)
    assert "jane@acme.com" in preview
    assert "Hello" in preview
    assert "Hi Jane" in preview


def test_style_structure_counts_paragraphs():
    body = "Hi Bob,\n\nFirst idea here with several words in it.\n\nSecond paragraph also has words.\n\nBest regards,\nAlex"
    st = extract_style_structure(body)
    assert st["n_paragraphs"] >= 3
    assert "Hi" in str(st["greeting"])
    assert "regards" in str(st["signoff"]).lower()


def test_enrichment_cache_skips_second_store_lookup():
    url = "https://www.linkedin.com/in/janedoe"
    payload = {
        "name": "Jane Doe",
        "title": "VP",
        "company": "Acme",
        "email": "jane@acme.com",
        "source": "zoominfo",
        "linkedin_url": url,
    }
    put_cached_enrichment(url, payload)
    hit = get_cached_enrichment("https://linkedin.com/in/janedoe/")
    assert hit and hit.get("email") == "jane@acme.com"
    assert normalize_linkedin_url(url) == normalize_linkedin_url(
        "https://www.linkedin.com/in/janedoe/"
    )
    india = "https://in.linkedin.com/in/george-mathew-31142b23"
    assert normalize_linkedin_url(india) == (
        "https://www.linkedin.com/in/george-mathew-31142b23"
    )


def test_enrichment_panel_and_labeled_fields():
    p = {
        "name": "Jane Doe",
        "title": "VP Sales",
        "company": "Acme",
        "email": "jane@acme.com",
        "phone": "111",
        "mobile": "222",
        "linkedin_url": "https://www.linkedin.com/in/janedoe",
        "source": "zoominfo",
        "industry": "Software",
        "location": "NY",
        "seniority": "VP",
        "about": "Leads sales.",
    }
    panel = format_enrichment_panel(p)
    assert "Prospect: Jane Doe, VP Sales at Acme" in panel
    assert "jane@acme.com" in panel
    assert "Mobile:   222" in panel
    assert "Location: NY" in panel
    assert "linkedin.com/in/janedoe" in panel
    assert "ZoomInfo" in panel
    fields = format_enrichment_fields(p)
    assert "Verified work email: jane@acme.com" in fields
    assert "Mobile: 222" in fields
    assert "LinkedIn: https://www.linkedin.com/in/janedoe" in fields


def test_explicit_recipient_lock_ignores_session_prospects():
    from agent.intent import looks_like_bulk_request, wants_prospect_list_recipients
    from agent.router import _build_draft_jobs

    msg = "draft to jane@acme.com about the CSR proposal"
    d = parse_directives(msg)
    assert d["explicit_recipient_lock"] is True
    assert d["to"].lower() == "jane@acme.com"
    assert d["to_list"] == ["jane@acme.com"]
    assert not wants_prospect_list_recipients(msg)
    assert not looks_like_bulk_request(msg)

    prospects = [
        {"email": f"{n}@acme.com", "name": n.title()}
        for n in ("jane", "bob", "carol", "dan", "eve")
    ]
    jobs = _build_draft_jobs(
        {"from_prospects": True, "batch": True, "subject": "Hi"},
        msg,
        prospects=prospects,
    )
    assert [j["recipient_email"].lower() for j in jobs] == ["jane@acme.com"]


def test_draft_to_with_cc_still_singular():
    d = parse_directives("draft to jane@acme.com cc bob@acme.com")
    assert d["to"].lower() == "jane@acme.com"
    assert "bob@acme.com" in [e.lower() for e in d["cc"]]
    assert d["explicit_recipient_lock"] is True


def test_like_sent_does_not_add_template_as_to():
    d = parse_directives(
        "draft to jane@acme.com like the one sent to alice@acme.com"
    )
    assert d["to"].lower() == "jane@acme.com"
    assert d["template_from"].lower() == "alice@acme.com"
    assert d["explicit_recipient_lock"] is True


def test_bulk_keywords_and_ask_ambiguity():
    from agent.intent import looks_like_bulk_request, wants_prospect_list_recipients

    assert looks_like_bulk_request("draft to all prospects")
    assert wants_prospect_list_recipients("draft to all prospects")
    assert not parse_directives("let's draft the outreach email")[
        "explicit_recipient_lock"
    ]
    assert not looks_like_bulk_request("let's draft the outreach email")


def test_draft_an_email_to_locks_recipient():
    d = parse_directives("draft an email to jane@acme.com")
    assert d["explicit_recipient_lock"] is True
    assert d["to"].lower() == "jane@acme.com"


def test_comma_separated_sent_to_locks_only_those_addresses():
    from agent.intent import parse_like_sent_request, wants_prospect_list_recipients
    from agent.router import _build_draft_jobs

    msg = (
        "sent to shilpi@csrbox.org, manasi@csrbox.org "
        "like email sent to lakshana@csrbox.org and cc rahul and deepti"
    )
    d = parse_directives(msg)
    tos = [e.lower() for e in d["to_list"]]
    assert tos == ["shilpi@csrbox.org", "manasi@csrbox.org"]
    assert d["template_from"].lower() == "lakshana@csrbox.org"
    assert d["explicit_recipient_lock"] is True
    assert "lakshana@csrbox.org" not in tos
    cc = [e.lower() for e in d["cc"]]
    assert "raahul.ppcm@gmail.com" in cc
    assert "deepti.87.srivastava@gmail.com" in cc
    like = parse_like_sent_request(msg)
    assert like and like["reference"].lower() == "lakshana@csrbox.org"
    assert not wants_prospect_list_recipients(msg)

    prospects = [
        {"email": f"{n}@csrbox.org", "name": n.title()}
        for n in ("lakshana", "other1", "other2", "shilpi")
    ]
    jobs = _build_draft_jobs(
        {"from_prospects": True, "batch": True, "subject": "Hi"},
        msg,
        prospects=prospects,
    )
    got = [j["recipient_email"].lower() for j in jobs]
    assert got == ["shilpi@csrbox.org", "manasi@csrbox.org"]


def test_like_sent_to_is_not_a_second_draft_recipient():
    from agent.intent import _heuristic_plan, parse_like_sent_request
    from agent.router import _build_draft_jobs

    msg = (
        "draft an email to chandrakant.kumbhani.ext@ambujacement.com "
        "and cc rahul and deepti and draft email like sent to "
        "gargi@smilefoundationindia.org and use attached file as attachment"
    )
    d = parse_directives(msg)
    tos = [e.lower() for e in d["to_list"]]
    assert tos == ["chandrakant.kumbhani.ext@ambujacement.com"]
    assert d["template_from"].lower() == "gargi@smilefoundationindia.org"
    assert "gargi@smilefoundationindia.org" not in tos
    like = parse_like_sent_request(msg)
    assert like and like["reference"].lower() == "gargi@smilefoundationindia.org"
    plan = _heuristic_plan(msg)
    assert [e.lower() for e in plan.to_emails] == [
        "chandrakant.kumbhani.ext@ambujacement.com"
    ]
    assert plan.like_sent_to.lower() == "gargi@smilefoundationindia.org"
    jobs = _build_draft_jobs(
        {"subject": "Hi", "html_body": "<p>x</p>"},
        msg,
        plan=plan,
    )
    assert [j["recipient_email"].lower() for j in jobs] == [
        "chandrakant.kumbhani.ext@ambujacement.com"
    ]
    assert not (plan.like_sent_for or "").strip() or "@" not in plan.like_sent_for
    from agent.intent import parse_explicit_draft_company, name_is_email_fragment

    assert parse_explicit_draft_company(msg) == ""
    assert name_is_email_fragment("chandrakant.kumbhani", msg)


def test_save_the_children_list_like_sent_does_not_drop_tos():
    from agent.intent import (
        _heuristic_plan,
        name_is_email_fragment,
        parse_explicit_draft_company,
        parse_like_sent_request,
    )
    from agent.router import _build_draft_jobs, _same_org_as_template, _wants_email_attachment

    expected = [
        "a_ansari@savethechildren.in",
        "annie.mathews@savethechildren.in",
        "a.dhar@savethechildren.in",
        "deepika.radhu@savethechildren.in",
        "ketaki.saksena@savethechildren.in",
        "k.jha@savethechildren.in",
        "madhumita.purkayastha@savethechildren.in",
        "pankaj.kumar@savethechildren.in",
        "puja.issar@savethechildren.in",
        "s_dhage@savethechildren.in",
        "s.malhotra@savethechildren.in",
        "subhashish.neogi@savethechildren.in",
        "surbhi.yadav@savethechildren.in",
    ]
    msg = (
        "draft an email to "
        + ",\n".join(expected)
        + "\n and cc rahul and deepti and draft email like sent to \t\n"
        "sheetal.srinivasamurthy@savethechildren.in and use attached as an attachment"
    )
    d = parse_directives(msg)
    tos = [e.lower() for e in d["to_list"]]
    assert tos == expected
    assert d["template_from"].lower() == "sheetal.srinivasamurthy@savethechildren.in"
    assert "sheetal.srinivasamurthy@savethechildren.in" not in tos
    cc = [e.lower() for e in d["cc"]]
    assert "sheetal.srinivasamurthy@savethechildren.in" not in cc
    assert "raahul.ppcm@gmail.com" in cc
    assert "deepti.87.srivastava@gmail.com" in cc
    assert parse_explicit_draft_company(msg) == ""
    assert name_is_email_fragment("sheetal", msg)
    like = parse_like_sent_request(msg)
    assert like and like["reference"].lower() == (
        "sheetal.srinivasamurthy@savethechildren.in"
    )
    assert not (like.get("target") or "").strip()
    plan = _heuristic_plan(msg)
    assert [e.lower() for e in plan.to_emails] == expected
    assert plan.like_sent_to.lower() == "sheetal.srinivasamurthy@savethechildren.in"
    assert not (plan.like_sent_for or "").strip()
    assert _same_org_as_template(plan.like_sent_to, expected)
    assert _wants_email_attachment(msg)

    prospects = [
        {"email": "other@acme.com", "name": "Other", "company": "Acme"},
        {
            "email": "sheetal.srinivasamurthy@savethechildren.in",
            "name": "Sheetal",
            "company": "Save the Children",
        },
    ]
    jobs = _build_draft_jobs(
        {
            "subject": "Hi",
            "html_body": "<p>x</p>",
            "from_prospects": True,
            "batch": True,
            "cc": plan.cc,
        },
        msg,
        prospects=prospects,
        plan=plan,
    )
    got = [j["recipient_email"].lower() for j in jobs]
    assert got == expected
    assert "sheetal.srinivasamurthy@savethechildren.in" not in got
    assert jobs[0].get("cc") == plan.cc

