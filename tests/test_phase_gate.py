# NOTE: Human gate — Phase 2 only on approved rows.
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_phase_gate_no_auto_advance(monkeypatch):
    import core.bulk_pipeline as bp

    saved = {}

    def save(job_id, job):
        saved[job_id] = job

    def load(job_id):
        return saved[job_id]

    monkeypatch.setattr(bp.drive_db, "save_bulk_job", save)
    monkeypatch.setattr(bp.drive_db, "load_bulk_job", load)
    monkeypatch.setattr(bp.drive_db, "list_bulk_jobs", lambda limit=50: [])
    monkeypatch.setattr(bp.drive_db, "update_bulk_row", lambda *a, **k: None)
    monkeypatch.setattr(bp.drive_db, "advance_job_phase", bp.drive_db.advance_job_phase)

    # use real advance with mocked get/put via save/load above — need patch drive_db internals
    store = {}

    def _put(name, payload):
        store[name] = payload

    def _get(name, default=None, use_cache=True):
        return store.get(name, default)

    monkeypatch.setattr(bp.drive_db, "_put", _put)
    monkeypatch.setattr(bp.drive_db, "_get", _get)

    jid = bp.create_enrichment_job(
        ["A", "B", "C"],
        persona_target={"titles": ["CSR"]},
        zi_credit_budget=50,
        gemini_token_budget=10000,
    )
    job = bp.get_bulk_job_status(jid)
    assert job["current_phase"] == "phase1_queued"
    # simulate phase1 done without starting phase2
    job["current_phase"] = "phase1_review"
    for r in job["rows"]:
        r["status"] = "ready_for_review"
    bp.drive_db.save_bulk_job(jid, job)

    spawned = []

    def fake_run_phase2(job_id):
        spawned.append(job_id)

    monkeypatch.setattr(bp, "run_phase2", fake_run_phase2)
    # approving should set phase2_queued but NOT auto-run unless we call run
    bp.approve_rows_for_phase2(jid, ["r001", "r002"], {"intent": "x"})
    job = bp.drive_db.load_bulk_job(jid)
    assert job["current_phase"] == "phase2_queued"
    assert spawned == []
    approved = [r for r in job["rows"] if r.get("approved_for_phase2")]
    assert len(approved) == 2
    assert {r["row_id"] for r in approved} == {"r001", "r002"}
