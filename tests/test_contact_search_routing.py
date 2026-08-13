# NOTE: Contact-search phrasing must route to ZoomInfo, not draft email.
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.intent import (
    _heuristic_plan,
    parse_contact_search_company,
    wants_contact_search,
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


def test_draft_still_drafts():
    msg = "draft an email to RateGain Travel Technologies"
    assert not wants_contact_search(msg)
    assert _heuristic_plan(msg).action == "draft_email"
