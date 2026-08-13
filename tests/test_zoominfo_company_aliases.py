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
    assert titles[0] == "CSR Head"
    assert "Head of CSR" in titles
    assert titles[0] == CSR_TITLE_PRIORITY[0]


def test_explicit_titles_kept_with_expand():
    titles, expand = _title_cascade_for_query(
        {"company_names": ["Acme"], "titles": ["CEO", "CFO"]}
    )
    assert titles == ["CEO", "CFO"]
    assert expand is True
