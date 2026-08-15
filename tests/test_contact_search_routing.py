# NOTE: Contact-search phrasing must route to ZoomInfo, not draft email.
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from connectors.zoominfo import (
    extract_linkedin_url,
    extract_linkedin_urls,
    linkedin_url_variants,
    names_from_linkedin_url,
    _linkedin_from_row,
    _linkedin_urls_match,
    _phone_from_row,
    _pick_contact_for_linkedin,
    _row_to_prospect,
)
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


def test_india_linkedin_url_extracts_and_routes_to_zoominfo():
    msg = (
        "find contact from linkedinurl: "
        "https://in.linkedin.com/in/george-mathew-31142b23"
    )
    urls = extract_linkedin_urls(msg)
    assert urls == ["https://www.linkedin.com/in/george-mathew-31142b23"]
    assert names_from_linkedin_url(urls[0]) == ("George", "Mathew")
    assert wants_linkedin_contact_lookup(msg)
    plan = _heuristic_plan(msg)
    assert plan.action == "prospect_enrich"
    assert "zoominfo" in plan.agents
    assert plan.draft is False


def test_linkedin_country_host_matches_www_slug():
    country = "https://in.linkedin.com/in/george-mathew-31142b23"
    www = "https://www.linkedin.com/in/george-mathew-31142b23/"
    assert _linkedin_urls_match(country, www)
    variants = linkedin_url_variants(country)
    assert "https://in.linkedin.com/in/george-mathew-31142b23" in variants
    assert "https://www.linkedin.com/in/george-mathew-31142b23" in variants


def test_linkedin_search_does_not_pick_namesake_with_email():
    asked = "https://www.linkedin.com/in/george-mathew-31142b23"
    contacts = [
        {
            "personId": "wrong",
            "hasEmail": True,
            "firstName": "George",
            "lastName": "Mathew",
            "externalUrls": [
                {"type": "linkedin", "url": "https://www.linkedin.com/in/other-george"}
            ],
        },
        {
            "personId": "right",
            "hasEmail": False,
            "firstName": "George",
            "lastName": "Mathew",
            "externalUrls": [{"type": "linkedin", "url": asked}],
        },
    ]
    picked = _pick_contact_for_linkedin(contacts, asked, require_match=True)
    assert picked and picked["personId"] == "right"
    namesake_only = _pick_contact_for_linkedin(
        contacts[:1], asked, require_match=True
    )
    assert namesake_only is None


def test_zoominfo_row_keeps_mobile_and_linkedin():
    row = {
        "id": "1",
        "firstName": "George",
        "lastName": "Mathew",
        "email": "george@example.com",
        "jobTitle": "Director",
        "companyName": "Acme",
        "mobilePhone": "+91 98765 43210",
        "externalUrls": [
            {"type": "LinkedIn", "value": "https://in.linkedin.com/in/george-mathew-31142b23"}
        ],
        "phoneList": [{"phone": "+91 98765 43210", "type": "mobile"}],
    }
    assert "george-mathew" in _linkedin_from_row(row).lower()
    phone, mobile = _phone_from_row(row)
    assert "98765" in mobile
    prospect = _row_to_prospect(row)
    assert "98765" in (prospect.get("mobile") or "")
    assert "linkedin.com/in/george-mathew" in (prospect.get("linkedin_url") or "")


def test_zoominfo_row_keeps_location():
    from connectors import prospect_location
    from connectors.zoominfo import _location_from_row, _row_to_prospect

    row = {
        "id": "1",
        "firstName": "Priya",
        "lastName": "Shah",
        "email": "priya@acme.com",
        "jobTitle": "CSR Head",
        "companyName": "Acme",
        "city": "Mumbai",
        "state": "Maharashtra",
        "country": "India",
        "metroRegion": "Mumbai Metro",
    }
    loc = _location_from_row(row)
    assert "Mumbai" in loc
    assert "India" in loc
    prospect = _row_to_prospect(row)
    assert "Mumbai" in (prospect.get("location") or "")
    assert prospect_location({"city": "Pune", "country": "India"}) == "Pune, India"


def test_search_hit_has_contact_email_or_mobile_flags():
    from connectors.zoominfo import _search_hit_has_contact

    assert _search_hit_has_contact({"hasEmail": True, "firstName": "A"})
    assert _search_hit_has_contact({"hasSupplementalEmail": True})
    assert _search_hit_has_contact({"hasMobilePhone": True})
    assert _search_hit_has_contact({"hasDirectPhone": True})
    assert _search_hit_has_contact({"email": "a@x.com"})
    assert _search_hit_has_contact({"mobilePhone": "+91 99999 11111"})
    assert not _search_hit_has_contact(
        {
            "id": "3",
            "firstName": "No",
            "lastName": "Contact",
            "hasEmail": False,
            "hasMobilePhone": False,
            "jobTitle": "CSR Head",
            "externalUrls": [
                {"type": "linkedin", "url": "https://linkedin.com/in/blank-person"}
            ],
        }
    )


def test_enrich_contact_rows_drops_blank_stubs():
    from connectors.zoominfo import ZoomInfoConnector

    zi = ZoomInfoConnector.__new__(ZoomInfoConnector)

    def fake_enrich(ids):
        out = []
        if "1" in [str(i) for i in ids]:
            out.append(
                {
                    "id": "1",
                    "firstName": "Asha",
                    "lastName": "Rao",
                    "email": "asha@example.org",
                    "jobTitle": "CSR Head",
                }
            )
        if "2" in [str(i) for i in ids]:
            out.append(
                {
                    "id": "2",
                    "firstName": "Blank",
                    "lastName": "Person",
                    "jobTitle": "CSR Head",
                }
            )
        return out

    zi._enrich_by_ids = fake_enrich
    rows = [
        {"id": "1", "hasEmail": True, "firstName": "Asha", "lastName": "Rao"},
        {
            "id": "2",
            "hasEmail": False,
            "firstName": "Blank",
            "lastName": "Person",
            "jobTitle": "CSR Head",
        },
    ]
    out = zi._enrich_contact_rows(rows, limit=5)
    assert len(out) == 1
    assert (out[0].get("email") or "") == "asha@example.org"

    # Flags omitted: still drop people enrich left without email or mobile
    rows_no_flags = [
        {"id": "1", "firstName": "Asha", "lastName": "Rao"},
        {"id": "2", "firstName": "Blank", "lastName": "Person"},
    ]
    out2 = zi._enrich_contact_rows(rows_no_flags, limit=5)
    assert len(out2) == 1
    assert (out2[0].get("email") or "") == "asha@example.org"


def test_enrich_contact_rows_keeps_mobile_only():
    from connectors.zoominfo import ZoomInfoConnector

    zi = ZoomInfoConnector.__new__(ZoomInfoConnector)

    def fake_enrich(ids):
        return [
            {
                "id": "9",
                "firstName": "Maya",
                "lastName": "Shah",
                "mobilePhone": "+91 99999 11111",
                "jobTitle": "Director",
            }
        ]

    zi._enrich_by_ids = fake_enrich
    rows = [
        {"id": "9", "hasMobilePhone": True, "firstName": "Maya", "lastName": "Shah"}
    ]
    out = zi._enrich_contact_rows(rows, limit=5)
    assert len(out) == 1
    blob = f"{out[0].get('mobile') or ''} {out[0].get('phone') or ''}"
    assert "99999" in blob


def test_search_all_skips_linkedin_only_rows():
    from connectors import prospects as prospects_mod

    class _Fake:
        def search(self, query, limit=10):
            return [
                {"name": "Has Mail", "email": "a@x.com", "source": "zoominfo"},
                {
                    "name": "LI only",
                    "email": "",
                    "mobile": "",
                    "linkedin_url": "https://linkedin.com/in/x",
                    "source": "zoominfo",
                },
                {"name": "Has Mobile", "email": "", "mobile": "+91 1", "source": "zoominfo"},
            ]

    orig = prospects_mod.get_connector
    prospects_mod.get_connector = lambda name: _Fake()
    try:
        rows = prospects_mod.search_all({"company_names": "Acme"}, limit_per_provider=10)
    finally:
        prospects_mod.get_connector = orig
    names = [r.get("name") for r in rows]
    assert "Has Mail" in names
    assert "Has Mobile" in names
    assert "LI only" not in names


def test_required_fields_are_email_or_mobile_not_both():
    from connectors.zoominfo import (
        _REACHABLE_REQUIRED_FIELDS,
        _build_contact_search_body,
        _with_required_field,
    )

    assert _REACHABLE_REQUIRED_FIELDS == ("email", "mobilePhone")
    assert all("," not in field for field in _REACHABLE_REQUIRED_FIELDS)
    base = _build_contact_search_body({"company_names": ["Acme"]}, limit=5)
    assert "requiredFields" not in base
    email_body = _with_required_field(base, "email")
    mobile_body = _with_required_field(base, "mobilePhone")
    assert email_body["requiredFields"] == "email"
    assert mobile_body["requiredFields"] == "mobilePhone"
    assert email_body["requiredFields"] != "email,mobilePhone"


def test_zoominfo_search_sends_required_fields_email_then_mobile():
    from connectors.zoominfo import ZoomInfoConnector, _REACHABLE_REQUIRED_FIELDS

    posted: list[dict] = []
    zi = ZoomInfoConnector.__new__(ZoomInfoConnector)
    zi._configured = lambda: True
    zi._search_companies = lambda query, limit=10: []
    zi._contacts_for_companies = lambda *a, **k: []

    def fake_rows(body, fallback_country=""):
        posted.append(dict(body))
        return []

    zi._search_contact_rows = fake_rows
    zi._enrich_contact_rows = lambda contacts, limit=10: []
    out = zi.search(
        {"company_names": ["Acme Corp"], "skip_web_csr": True},
        limit=5,
    )
    assert out == []
    fields = [b.get("requiredFields") for b in posted]
    assert fields == list(_REACHABLE_REQUIRED_FIELDS)
    assert all("," not in (f or "") for f in fields)
    assert all(b.get("companyName") for b in posted)


