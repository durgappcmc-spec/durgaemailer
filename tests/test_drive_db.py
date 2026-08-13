# NOTE: drive_db unit tests with mocked drive_store.
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def mem_drive(monkeypatch, tmp_path):
    store: dict[str, Any] = {}

    def upload(name: str, payload: Any) -> bool:
        store[name] = payload
        return True

    def download(name: str):
        return store.get(name)

    import core.drive_store as ds
    import core.drive_db as db

    monkeypatch.setattr(ds, "upload_json", upload)
    monkeypatch.setattr(ds, "download_json", download)
    monkeypatch.setattr(db, "_CACHE_DIR", tmp_path / "cache")
    db.invalidate_cache()
    return store, db


def test_bulk_job_crud_and_row_update(mem_drive):
    store, db = mem_drive
    job = {
        "job_id": "bulk_test",
        "created_at": "2026-01-01T00:00:00Z",
        "current_phase": "phase1_queued",
        "rows": [{"row_id": "r001", "status": "queued", "input": "Pratham"}],
        "totals": {},
        "config": {},
    }
    db.save_bulk_job("bulk_test", job)
    loaded = db.load_bulk_job("bulk_test")
    assert loaded["rows"][0]["input"] == "Pratham"
    db.update_bulk_row("bulk_test", "r001", {"status": "ready_for_review"})
    assert db.load_bulk_job("bulk_test")["rows"][0]["status"] == "ready_for_review"
    assert db.list_bulk_jobs(limit=5)[0]["job_id"] == "bulk_test"


def test_advance_phase_sets_approved(mem_drive):
    _, db = mem_drive
    db.save_bulk_job(
        "j2",
        {
            "job_id": "j2",
            "rows": [
                {"row_id": "a", "status": "ready_for_review"},
                {"row_id": "b", "status": "ready_for_review"},
            ],
            "totals": {},
        },
    )
    db.advance_job_phase("j2", "phase2_queued", ["a"], {"intent": "x"})
    job = db.load_bulk_job("j2")
    assert job["current_phase"] == "phase2_queued"
    assert job["rows"][0]["approved_for_phase2"] is True
    assert job["rows"][1].get("approved_for_phase2") in (False, None)


def test_trace_append_tail(mem_drive):
    _, db = mem_drive
    db.append_trace_event("s1", {"type": "plan"})
    db.append_trace_event("s1", {"type": "tool_call"})
    events = db.load_trace("s1")
    assert events[0]["seq"] == 1
    assert events[1]["seq"] == 2
    assert len(db.tail_trace("s1", 1)) == 1


def test_gemini_usage_mtd(mem_drive):
    _, db = mem_drive
    db.log_gemini_call({"task_kind": "contact_planner", "tokens_in": 10, "tokens_out": 5})
    db.log_gemini_call({"task_kind": "compose_email", "tokens_in": 100, "tokens_out": 50})
    db.log_gemini_call({"task_kind": "contact_planner", "tokens_in": 7, "tokens_out": 3})
    usage = db.gemini_usage_mtd()
    assert usage["totals"]["calls"] == 3
    assert usage["by_task_kind"]["contact_planner"]["calls"] == 2
    assert usage["by_task_kind"]["compose_email"]["tokens_out"] == 50


def test_cache_invalidation(mem_drive):
    store, db = mem_drive
    db.save_persona_targets([{"id": "x"}])
    assert db.load_persona_targets()[0]["id"] == "x"
    store["DurgaEmailer/persona_targets.json"] = [{"id": "y"}]
    db.invalidate_cache("DurgaEmailer/persona_targets.json")
    assert db.load_persona_targets()[0]["id"] == "y"
