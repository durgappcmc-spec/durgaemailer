# NOTE: Alias / rank helpers for Sterlite-style company-first ZoomInfo search.
from __future__ import annotations

from connectors.zoominfo import (
    CSR_TITLE_PRIORITY,
    _rank_companies_for_query,
    _title_cascade_for_query,
    _with_company_name_aliases,
)


def test_sterlite_tech_expands_legal_name_and_domain():
    q = _with_company_name_aliases({"company_names": ["sterlite tech"]})
    names = [n.lower() for n in q["company_names"]]
    assert "sterlite tech" in names
    assert any("sterlite technologies" in n for n in names)
    domains = [d.lower() for d in q.get("company_domains") or []]
    assert "sterlitetech.com" in domains


def test_rank_prefers_sterlite_technologies():
    firms = [
        {"id": "1", "name": "Sterlite Power"},
        {"id": "2", "name": "Sterlite Technologies Limited"},
        {"id": "3", "name": "Acme Sterile Labs"},
    ]
    ranked = _rank_companies_for_query(firms, "sterlite tech")
    assert ranked[0]["id"] == "2"


def test_company_search_uses_csr_title_cascade_then_expand():
    titles, expand = _title_cascade_for_query({"company_names": ["sterlite tech"]})
    assert expand is True
    assert titles[0] in ("Head CSR", "CSR Head", "Head of CSR")
    assert any("Head CSR" == t or "CSR" == t for t in titles)
    assert "Head CSR" in titles or "CSR Head" in titles


def test_contact_relevance_prefers_csr_stl():
    from connectors.zoominfo import _contact_relevance_key

    rows = [
        {"name": "Random", "title": "Engineer", "email": "x@other.com"},
        {
            "name": "Anupam Das",
            "title": "Head CSR & Sustainability",
            "email": "anupam.das@stl.tech",
        },
        {
            "name": "Swati Bhattacharya",
            "title": "Chief Marketing Officer & Head CSR",
            "email": "swati.bhattacharya@stl.tech",
        },
    ]
    ranked = sorted(rows, key=_contact_relevance_key)
    assert ranked[0]["name"] in ("Anupam Das", "Swati Bhattacharya")
    assert all("stl.tech" in (r.get("email") or "") for r in ranked[:2])


def test_explicit_titles_kept_with_expand():
    titles, expand = _title_cascade_for_query(
        {"company_names": ["Acme"], "titles": ["CEO", "CFO"]}
    )
    assert titles == ["CEO", "CFO"]
    assert expand is True


def test_room_to_read_is_nonprofit_not_csr_ladder():
    from connectors.zoominfo import _is_nonprofit_query

    q = {"company_names": ["Room to Read"]}
    assert _is_nonprofit_query(q) is True
    titles, expand = _title_cascade_for_query(q)
    assert expand is True
    assert titles[0] == "Founder"
    assert "Head CSR" not in titles[:3]


def test_learning_links_alias_prefers_india_foundation():
    q = _with_company_name_aliases({"company_names": ["Learning Links"]})
    names = [n.lower() for n in q["company_names"]]
    assert any("foundation" in n for n in names)
    domains = [d.lower() for d in q.get("company_domains") or []]
    assert "learninglinksindia.org" in domains


def test_rank_drops_reading_room_namesake():
    firms = [
        {"id": "1", "name": "Reading Room", "website": "www.readingroom.com"},
        {
            "id": "2",
            "name": "Room to Read",
            "website": "www.roomtoread.org",
            "country": "United States",
        },
    ]
    ranked = _rank_companies_for_query(
        firms, "Room to Read", domains=["roomtoread.org"]
    )
    assert ranked[0]["id"] == "2"
    assert all("reading room" not in (c.get("name") or "").lower() for c in ranked)


def test_rank_prefers_learning_links_foundation_india():
    firms = [
        {
            "id": "1",
            "name": "Learning Links",
            "website": "www.learninglinks.co.uk",
            "country": "United Kingdom",
        },
        {
            "id": "2",
            "name": "Learning Links Foundation",
            "website": "www.learninglinksindia.org",
            "country": "India",
        },
    ]
    ranked = _rank_companies_for_query(
        firms, "Learning Links", domains=["learninglinksindia.org"]
    )
    assert ranked[0]["id"] == "2"


def test_room_to_read_india_not_treated_as_geo_or_wrong_firm():
    from connectors.zoominfo import (
        _build_contact_search_body,
        _geo_filters,
        _guess_country,
        _with_company_name_aliases,
    )

    q = {"company_names": ["Room to Read India"]}
    assert _guess_country(q) == ""
    assert "country" not in _geo_filters(q)
    body = _build_contact_search_body(q, limit=5)
    assert body.get("companyName")
    assert "industryKeywords" not in body
    assert "country" not in body
    aliased = _with_company_name_aliases(q)
    assert "roomtoreadindia.org" in [d.lower() for d in aliased.get("company_domains") or []]

    firms = [
        {
            "id": "azad",
            "name": "Azad Reading Room - India",
            "website": "www.azadreadingroom.info",
            "country": "India",
        },
        {
            "id": "rtr",
            "name": "Room To Read India",
            "website": "www.roomtoreadindia.org",
        },
    ]
    ranked = _rank_companies_for_query(
        firms, "Room to Read India", domains=["roomtoreadindia.org"]
    )
    assert ranked[0]["id"] == "rtr"
    assert all("azad" not in (c.get("name") or "").lower() for c in ranked)


def test_fields_without_invalid_strips_disallowed():
    from connectors.zoominfo import _fields_without_invalid

    class _Resp:
        def json(self):
            return {"invalidOutputFields": ["metroRegion", "bio"]}

    out = _fields_without_invalid(
        ["id", "email", "metroRegion", "bio", "jobTitle"], _Resp()
    )
    assert out == ["id", "email", "jobTitle"]

