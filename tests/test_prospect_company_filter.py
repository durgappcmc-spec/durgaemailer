# NOTE: Reject marketplace emails polluted onto a company search.
from core.prospect_list import (
    email_blocked_for_company_search,
    filter_prospects_for_company_query,
    prospect_fits_company_query,
)


def test_indiamart_blocked_for_sterlite():
    assert email_blocked_for_company_search("khurshidalam.qureshi@indiamart.com")
    bad = {
        "name": "Khurshidalam Qureshi",
        "email": "khurshidalam.qureshi@indiamart.com",
        "company": "sterlite tech",
        "source": "zoominfo",
    }
    assert not prospect_fits_company_query(bad, ["sterlite tech"])
    assert filter_prospects_for_company_query([bad], ["sterlite tech"]) == []


def test_real_sterlite_email_kept():
    good = {
        "name": "Priya Shah",
        "email": "priya.shah@sterlitetech.com",
        "company": "Sterlite Technologies",
        "source": "zoominfo",
    }
    assert prospect_fits_company_query(good, ["sterlite tech"])
    assert not email_blocked_for_company_search(good["email"])
