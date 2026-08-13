# NOTE: 30-row mocked job — approve 24 — Phase 2 only on those.
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_bulk_pipeline_phase_split(monkeypatch):
    import core.bulk_pipeline as bp

    store = {}

    def _put(name, payload):
        store[name] = payload

    def _get(name, default=None, use_cache=True):
        return store.get(name, default)

    monkeypatch.setattr(bp.drive_db, "_put", _put)
    monkeypatch.setattr(bp.drive_db, "_get", _get)

    names = [f"Org{i}" for i in range(30)]
    jid = bp.create_enrichment_job(
        names,
        persona_target={"titles": ["CSR Head"]},
        zi_credit_budget=100,
        gemini_token_budget=200000,
        concurrency=1,
    )
    job = bp.drive_db.load_bulk_job(jid)
    assert len(job["rows"]) == 30

    # Simulate phase1 outcomes
    for i, r in enumerate(job["rows"]):
        if i < 27:
            r["status"] = "ready_for_review"
            r["contact"] = {"name": f"N{i}", "email": f"n{i}@x.org", "title": "CSR"}
        elif i < 29:
            r["status"] = "failed"
        else:
            r["status"] = "running"
    job["current_phase"] = "phase1_review"
    bp.drive_db.save_bulk_job(jid, job)

    approve_ids = [r["row_id"] for r in job["rows"] if r["status"] == "ready_for_review"][:24]
    bp.approve_rows_for_phase2(jid, approve_ids, {"intent": "partnership_outreach", "tracking": True})
    job = bp.drive_db.load_bulk_job(jid)
    approved = [r for r in job["rows"] if r.get("approved_for_phase2")]
    assert len(approved) == 24
    assert job["current_phase"] == "phase2_queued"

    ran = []

    class FakeDraft:
        def __init__(self, **kwargs):
            self.persist = kwargs.get("persist_row")

        def run(self, row, *, extras=None):
            ran.append(row.row_id)
            row.status = "ready"
            row.draft_id = f"d_{row.row_id}"
            row.tracking_id = f"t_{row.row_id}"
            if self.persist:
                self.persist(row)
            return row

    monkeypatch.setattr(bp, "DraftAgent", FakeDraft)
    monkeypatch.setattr(bp, "build_registry", lambda: object())
    monkeypatch.setattr(bp, "get_gemini_client", lambda: object())
    # Avoid real thread semaphores blocking — run_phase2 sync
    bp._CANCEL_FLAGS.discard(jid)
    bp._PAUSE_FLAGS.discard(jid)
    bp._RUNNING_JOBS.discard(jid)
    bp.run_phase2(jid)
    assert len(ran) == 24
    job = bp.drive_db.load_bulk_job(jid)
    tracked = [r for r in job["rows"] if r.get("tracking_id")]
    assert len(tracked) == 24
