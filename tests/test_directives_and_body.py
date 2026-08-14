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


def test_enrichment_panel_and_labeled_fields():
    p = {
        "name": "Jane Doe",
        "title": "VP Sales",
        "company": "Acme",
        "email": "jane@acme.com",
        "phone": "",
        "source": "zoominfo",
        "industry": "Software",
        "location": "NY",
        "seniority": "VP",
        "about": "Leads sales.",
    }
    panel = format_enrichment_panel(p)
    assert "Prospect: Jane Doe, VP Sales at Acme" in panel
    assert "jane@acme.com" in panel
    assert "ZoomInfo" in panel
    fields = format_enrichment_fields(p)
    assert "Verified work email: jane@acme.com" in fields
    assert "{" not in fields.split("Verified")[0]
