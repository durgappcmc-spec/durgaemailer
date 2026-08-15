# NOTE: Reject marketplace emails polluted onto a company search.
from core.prospect_list import (
    _prospect_key,
    delete_prospects,
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


def test_delete_prospects_drops_selected_keys(monkeypatch):
    rows = [
        {"name": "Ann", "email": "ann@acme.com", "company": "Acme"},
        {"name": "Bob", "email": "bob@acme.com", "company": "Acme"},
        {"name": "Cara", "email": "cara@beta.com", "company": "Beta"},
    ]
    store = {"rows": list(rows)}

    monkeypatch.setattr(
        "core.prospect_list._load", lambda: store["rows"]
    )

    def fake_persist(new_rows):
        store["rows"] = list(new_rows)
        return True

    monkeypatch.setattr("core.prospect_list._persist", fake_persist)
    n = delete_prospects(
        [_prospect_key(rows[0]), _prospect_key(rows[2])]
    )
    assert n == 2
    assert [r["email"] for r in store["rows"]] == ["bob@acme.com"]


def test_delete_prospects_empty_is_noop(monkeypatch):
    store = {"rows": [{"name": "Ann", "email": "ann@acme.com"}]}
    monkeypatch.setattr("core.prospect_list._load", lambda: store["rows"])
    monkeypatch.setattr(
        "core.prospect_list._persist",
        lambda _rows: (_ for _ in ()).throw(AssertionError("persist")),
    )
    assert delete_prospects([]) == 0
    assert delete_prospects(["", "  "]) == 0
    assert store["rows"][0]["email"] == "ann@acme.com"


def test_save_prospects_requires_email_or_mobile(monkeypatch):
    from core.prospect_list import save_prospects

    store = {"rows": []}
    monkeypatch.setattr("core.prospect_list._load", lambda: store["rows"])

    def fake_persist(new_rows):
        store["rows"] = list(new_rows)
        return True

    monkeypatch.setattr("core.prospect_list._persist", fake_persist)
    n = save_prospects(
        [
            {"name": "No Contact", "company": "Acme", "linkedin_url": "https://linkedin.com/in/x"},
            {"name": "Email Only", "email": "a@acme.com", "company": "Acme"},
            {"name": "Mobile Only", "mobile": "+91 99999", "company": "Acme"},
            {"name": "Phone Only", "phone": "+91 88888", "company": "Acme"},
        ]
    )
    assert n == 3
    emails = {(r.get("email") or "") for r in store["rows"]}
    mobiles = {(r.get("mobile") or r.get("phone") or "") for r in store["rows"]}
    names = {r.get("name") for r in store["rows"]}
    assert "No Contact" not in names
    assert "Email Only" in names
    assert "Mobile Only" in names
    assert "Phone Only" in names
    assert "a@acme.com" in emails
    assert "+91 99999" in mobiles or "+91 88888" in mobiles


def test_visible_prospects_hides_rows_without_email_or_mobile(monkeypatch):
    from core.prospect_list import visible_prospects

    monkeypatch.setattr(
        "core.prospect_list._load",
        lambda: [
            {"name": "LinkedIn only", "company": "Acme", "linkedin_url": "https://x"},
            {"name": "Ann", "email": "ann@acme.com", "company": "Acme"},
        ],
    )
    rows = visible_prospects()
    assert [r.get("name") for r in rows] == ["Ann"]

