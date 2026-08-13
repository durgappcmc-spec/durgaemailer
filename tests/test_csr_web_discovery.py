# NOTE: Unit tests for Google/LinkedIn → ZoomInfo CSR discovery helpers.
from __future__ import annotations

from agent.csr_web_discovery import (
    _extract_json_object,
    _merge_web_and_zi,
    _normalize_linkedin,
)


def test_normalize_linkedin():
    url = _normalize_linkedin("https://www.linkedin.com/in/anupam-das-stl/")
    assert "linkedin.com/in/anupam-das-stl" in url


def test_extract_json_from_fenced():
    text = '```json\n{"contacts":[{"name":"A","linkedin_url":"https://linkedin.com/in/a"}]}\n```'
    blob = _extract_json_object(text)
    assert blob["contacts"][0]["name"] == "A"


def test_prefer_google_csr_title_over_wrong_zi():
    web = {
        "name": "Anupam Das",
        "title": "Head CSR & Sustainability",
        "linkedin_url": "https://www.linkedin.com/in/anupam-das",
    }
    zi = {
        "name": "Anupam Das",
        "title": "Manager",  # stale / wrong on ZoomInfo
        "email": "anupam.das@stl.tech",
        "company": "Sterlite Technologies",
        "linkedin_url": "",
    }
    merged = _merge_web_and_zi(web, zi, company="Sterlite Tech")
    assert merged is not None
    assert "CSR" in (merged.get("title") or "")
    assert merged.get("email") == "anupam.das@stl.tech"
    assert "linkedin.com/in/anupam-das" in (merged.get("linkedin_url") or "")
    assert merged.get("zi_title") == "Manager"
