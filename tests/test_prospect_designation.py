# NOTE: Tracking Follow-ups / Hot Leads show job title from send row or Saved prospects.
from core.prospect_list import designation_for_row, titles_by_email


def test_designation_prefers_send_row_title():
    titles = {"a@x.com": "Saved Title"}
    row = {
        "recipient_email": "a@x.com",
        "title": "Country Director",
    }
    assert designation_for_row(row, titles) == "Country Director"


def test_designation_falls_back_to_prospect_lookup():
    titles = {"a@x.com": "Head of CSR"}
    row = {"recipient_email": "A@x.com", "subject": "Hello"}
    assert designation_for_row(row, titles) == "Head of CSR"


def test_designation_empty_when_unknown():
    assert designation_for_row({"recipient_email": "nobody@x.com"}, {}) == ""
    assert designation_for_row({}, {"a@x.com": "VP"}) == ""


def test_titles_by_email_uses_title_or_designation():
    rows = [
        {"email": "a@x.com", "title": "CEO"},
        {"email": "b@x.com", "designation": "Director, Finance"},
        {"email": "a@x.com", "title": "Ignored duplicate"},
        {"email": "", "title": "No email"},
    ]
    out = titles_by_email(rows)
    assert out["a@x.com"] == "CEO"
    assert out["b@x.com"] == "Director, Finance"
    assert len(out) == 2
