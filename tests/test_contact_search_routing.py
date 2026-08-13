# NOTE: Contact-search phrasing must route to ZoomInfo, not draft email.
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from connectors.zoominfo import extract_linkedin_url, extract_linkedin_urls
from agent.intent import (
    _heuristic_plan,
    parse_contact_search_company,
    parse_like_sent_request,
    parse_named_person_contact,
    wants_contact_search,
    wants_linkedin_contact_lookup,
    wants_live_zoominfo_search,
    wants_saved_list_only,
    wants_search_then_draft,
)


def test_rategain_contact_search_phrase():
    msg = "search for contact from RateGain Travel Technologies"
    assert wants_contact_search(msg)
    assert parse_contact_search_company(msg) == "RateGain Travel Technologies"
    plan = _heuristic_plan(msg)
    assert plan.action == "prospect_search"
    assert plan.draft is False


def test_find_contacts_at_company():
    msg = "find contacts at Magic Bus"
    assert wants_contact_search(msg)
    assert "Magic Bus" in parse_contact_search_company(msg)
    assert _heuristic_plan(msg).action == "prospect_search"


def test_sterlite_forces_live_zoominfo():
    msg = "search contact from Sterlite Tech"
    assert wants_contact_search(msg)
    assert parse_contact_search_company(msg) == "Sterlite Tech"
    assert wants_live_zoominfo_search(msg)
    assert not wants_saved_list_only(msg)
    assert _heuristic_plan(msg).action == "prospect_search"


def test_saved_list_phrasing_skips_live_zoom():
    msg = "show saved contacts from Sterlite Tech"
    assert wants_saved_list_only(msg)
    assert not wants_live_zoominfo_search(msg)


def test_named_person_from_domain_goes_to_enrich():
    msg = "check for contact of Saswati Swain from soprasteria.com"
    assert wants_contact_search(msg)
    parsed = parse_named_person_contact(msg)
    assert parsed.get("first_name") == "Saswati"
    assert parsed.get("last_name") == "Swain"
    assert parsed.get("company_domain") == "soprasteria.com"
    plan = _heuristic_plan(msg)
    assert plan.action == "prospect_enrich"
    assert plan.draft is False


def test_draft_still_drafts():
    msg = "draft an email to RateGain Travel Technologies"
    assert not wants_contact_search(msg)
    assert _heuristic_plan(msg).action == "draft_email"


def test_pure_sterlite_search_no_like_sent_bleed():
    """Earlier IndiaMART like-sent in history must not attach to a plain search."""
    msg = "search contacts from sterlite tech"
    plan = _heuristic_plan(msg)
    assert plan.action == "prospect_search"
    assert plan.draft is False
    assert plan.like_sent_to == ""
    assert plan.like_sent_for == ""
    assert plan.like_sent_message_id == ""
    assert plan.agents == ["zoominfo"]


def test_search_then_like_sent_still_keeps_draft_flags():
    msg = (
        "search contacts from sterlite tech and create draft email "
        "like khurshidalam.qureshi@indiamart.com"
    )
    assert wants_contact_search(msg)
    assert wants_search_then_draft(msg)
    assert parse_contact_search_company(msg).lower().startswith("sterlite")
    like = parse_like_sent_request(msg)
    assert like is not None
    assert like["reference"].lower() == "khurshidalam.qureshi@indiamart.com"
    plan = _heuristic_plan(msg)
    assert plan.action == "prospect_search"
    assert plan.draft is True
    assert plan.like_sent_to.lower() == "khurshidalam.qureshi@indiamart.com"
    assert "sterlite" in (plan.like_sent_for or "").lower()
    assert "gmail" in plan.agents



def test_pure_like_sent_still_drafts():
    msg = "create draft email like khurshidalam.qureshi@indiamart.com for Flipkart"
    assert not wants_contact_search(msg)
    assert not wants_search_then_draft(msg)
    plan = _heuristic_plan(msg)
    assert plan.action == "draft_email"
    assert "flipkart" in (plan.like_sent_for or "").lower()


def test_extract_all_pasted_linkedin_urls():
    msg = (
        "get contacts for these linkedin profiles\n"
        "https://www.linkedin.com/in/sushmita-esg/\n"
        "linkedin.com/in/priya-sharma-csr\n"
        "www.linkedin.com/in/anupam-das-stl/\n"
        "https://www.linkedin.com/in/sushmita-esg/\n"
    )
    urls = extract_linkedin_urls(msg)
    assert len(urls) == 3
    assert urls[0].endswith("/sushmita-esg")
    assert urls[1].endswith("/priya-sharma-csr")
    assert urls[2].endswith("/anupam-das-stl")
    assert extract_linkedin_url(msg).endswith("/sushmita-esg")


def test_get_contacts_for_linkedin_profiles_routes_to_enrich():
    msg = (
        "get contacts for these linkedin profiles "
        "https://www.linkedin.com/in/sushmita-esg/ "
        "https://linkedin.com/in/priya-sharma-csr"
    )
    assert wants_linkedin_contact_lookup(msg)
    assert wants_contact_search(msg)
    plan = _heuristic_plan(msg)
    assert plan.action == "prospect_enrich"
    assert "zoominfo" in plan.agents
    assert plan.draft is False


def test_linkedin_profiles_then_draft_keeps_enrich_first():
    msg = (
        "get contacts for these linkedin profiles and then draft personalized "
        "emails https://linkedin.com/in/a-one https://linkedin.com/in/b-two"
    )
    assert wants_linkedin_contact_lookup(msg)
    assert wants_search_then_draft(msg)
    plan = _heuristic_plan(msg)
    assert plan.action == "prospect_enrich"
    assert plan.draft is True
    assert "gmail" in plan.agents


def test_collect_linkedin_urls_from_prior_user_paste():
    from agent.router import _collect_linkedin_profile_urls

    history = [
        {
            "role": "user",
            "content": (
                "https://www.linkedin.com/in/one-person\n"
                "https://www.linkedin.com/in/two-person"
            ),
        }
    ]
    urls = _collect_linkedin_profile_urls(
        "get contacts for these linkedin profiles", history
    )
    assert len(urls) == 2
    assert urls[0].endswith("/one-person")
    assert urls[1].endswith("/two-person")
