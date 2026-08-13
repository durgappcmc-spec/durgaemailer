# NOTE: Thin two-phase bulk orchestrator — no per-row pipelines.
from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Optional

from core.agent.contact_agent import ContactAgent
from core.agent.draft_agent import DraftAgent
from core.agent.planner_base import RowState
from core.agent.gemini_client import get_gemini_client
from core import drive_db
from core.tools import build_registry

_ZI_SEM = threading.Semaphore(1)
_GEM_SEM = threading.Semaphore(1)
_WORKER_LOCK = threading.Lock()
_RUNNING_JOBS: set[str] = set()
_PAUSE_FLAGS: set[str] = set()
_CANCEL_FLAGS: set[str] = set()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def create_enrichment_job(
    company_inputs: list[str],
    persona_target: dict,
    zi_credit_budget: int,
    gemini_token_budget: int,
    concurrency: int = 1,
) -> str:
    job_id = f"bulk_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}_{uuid.uuid4().hex[:4]}"
    rows = []
    for i, raw in enumerate(company_inputs):
        text = (raw or "").strip()
        if not text:
            continue
        rows.append(
            RowState(
                row_id=f"r{i+1:03d}",
                phase="phase1",
                input=text,
                status="queued",
            ).model_dump(mode="json")
        )
    job = {
        "job_id": job_id,
        "created_at": _now(),
        "current_phase": "phase1_queued",
        "persona_target": persona_target or {},
        "phase2_config": None,
        "totals": {
            "phase1": {
                "queued": len(rows),
                "done": 0,
                "failed": 0,
                "in_progress": 0,
            },
            "phase2": {
                "approved": 0,
                "done": 0,
                "failed": 0,
                "in_progress": 0,
            },
        },
        "rows": rows,
        "config": {
            "concurrency": max(1, min(3, int(concurrency or 1))),
            "zi_credit_budget": int(zi_credit_budget),
            "zi_credits_used": 0,
            "gemini_token_budget": int(gemini_token_budget),
            "gemini_tokens_used_by_task_kind": {
                "contact_planner": {"in": 0, "out": 0},
                "draft_planner": {"in": 0, "out": 0},
                "org_brief_synth": {"in": 0, "out": 0},
                "compose_email": {"in": 0, "out": 0},
                "grounding_check": {"in": 0, "out": 0},
            },
            "retry_policy": {"max_retries": 2, "backoff": "exp"},
        },
    }
    drive_db.save_bulk_job(job_id, job)
    return job_id


def get_bulk_job_status(job_id: str) -> dict:
    job = drive_db.load_bulk_job(job_id)
    _recompute_totals(job)
    return job


def pause_job(job_id: str) -> None:
    _PAUSE_FLAGS.add(job_id)
    job = drive_db.load_bulk_job(job_id)
    phase = job.get("current_phase") or ""
    if phase.endswith("_running"):
        job["current_phase"] = phase.replace("_running", "_paused")
        drive_db.save_bulk_job(job_id, job)


def resume_job(job_id: str) -> None:
    _PAUSE_FLAGS.discard(job_id)
    job = drive_db.load_bulk_job(job_id)
    phase = job.get("current_phase") or ""
    if "phase1" in phase:
        job["current_phase"] = "phase1_running"
        drive_db.save_bulk_job(job_id, job)
        run_phase1(job_id)
    elif "phase2" in phase:
        job["current_phase"] = "phase2_running"
        drive_db.save_bulk_job(job_id, job)
        run_phase2(job_id)


def cancel_job(job_id: str) -> None:
    _CANCEL_FLAGS.add(job_id)
    _PAUSE_FLAGS.add(job_id)
    job = drive_db.load_bulk_job(job_id)
    job["current_phase"] = "cancelled"
    drive_db.save_bulk_job(job_id, job)


def approve_rows_for_phase2(
    job_id: str, approved_row_ids: list[str], phase2_config: dict
) -> None:
    drive_db.advance_job_phase(
        job_id,
        new_phase="phase2_queued",
        approved_row_ids=approved_row_ids,
        phase2_config=phase2_config or {},
    )


def retry_failed_rows(job_id: str, phase: str) -> None:
    job = drive_db.load_bulk_job(job_id)
    for row in job.get("rows") or []:
        if phase == "phase1" and row.get("status") == "failed":
            row["status"] = "queued"
            row["status_message"] = ""
            row["step"] = 0
        if phase == "phase2" and row.get("approved_for_phase2") and row.get("status") == "failed":
            row["status"] = "queued"
            row["status_message"] = ""
            row["step"] = 0
            row["phase"] = "phase2"
    drive_db.save_bulk_job(job_id, job)
    if phase == "phase1":
        run_phase1(job_id)
    else:
        run_phase2(job_id)


def run_phase1(job_id: str) -> None:
    with _WORKER_LOCK:
        if job_id in _RUNNING_JOBS:
            return
        _RUNNING_JOBS.add(job_id)
    try:
        _run_phase(job_id, phase="phase1")
    finally:
        with _WORKER_LOCK:
            _RUNNING_JOBS.discard(job_id)


def run_phase2(job_id: str) -> None:
    with _WORKER_LOCK:
        if job_id in _RUNNING_JOBS:
            return
        _RUNNING_JOBS.add(job_id)
    try:
        _run_phase(job_id, phase="phase2")
    finally:
        with _WORKER_LOCK:
            _RUNNING_JOBS.discard(job_id)


def _run_phase(job_id: str, *, phase: str) -> None:
    job = drive_db.load_bulk_job(job_id)
    job["current_phase"] = f"{phase}_running"
    drive_db.save_bulk_job(job_id, job)
    registry = build_registry()
    gemini = get_gemini_client()
    concurrency = int((job.get("config") or {}).get("concurrency") or 1)
    persona = job.get("persona_target") or {}
    phase2_config = job.get("phase2_config") or {}

    def persist(row: RowState) -> None:
        drive_db.update_bulk_row(job_id, row.row_id, row.model_dump(mode="json"))
        # refresh job budgets
        try:
            j = drive_db.load_bulk_job(job_id)
            j["config"]["zi_credits_used"] = sum(
                int(r.get("zi_credits_used") or 0) for r in (j.get("rows") or [])
            )
            _recompute_totals(j)
            drive_db.save_bulk_job(job_id, j)
        except Exception:
            pass

    def work(row_dict: dict) -> None:
        if job_id in _CANCEL_FLAGS or job_id in _PAUSE_FLAGS:
            return
        j = drive_db.load_bulk_job(job_id)
        cfg = j.get("config") or {}
        if int(cfg.get("zi_credits_used") or 0) >= int(cfg.get("zi_credit_budget") or 10**9):
            drive_db.update_bulk_row(
                job_id,
                row_dict["row_id"],
                {"status": "paused_budget", "status_message": "job zi budget"},
            )
            return
        used_tokens = sum(int(r.get("gemini_tokens_used") or 0) for r in (j.get("rows") or []))
        if used_tokens >= int(cfg.get("gemini_token_budget") or 10**12):
            drive_db.update_bulk_row(
                job_id,
                row_dict["row_id"],
                {"status": "paused_budget", "status_message": "job gemini budget"},
            )
            return

        row = RowState.model_validate(row_dict)
        row.phase = phase  # type: ignore[assignment]
        if phase == "phase1":
            agent = ContactAgent(
                registry=registry,
                gemini=gemini,
                job_id=job_id,
                persist_row=persist,
                persona_target=persona,
            )
        else:
            if not row.approved_for_phase2:
                return
            agent = DraftAgent(
                registry=registry,
                gemini=gemini,
                job_id=job_id,
                persist_row=persist,
                phase2_config=phase2_config,
            )
        # Serialize ZI/Gemini at tool layer via semaphores wrapping agent.run sections:
        # agent loop itself is the unit; acquire briefly around run start.
        with _ZI_SEM:
            with _GEM_SEM:
                agent.run(row)

    # select rows
    targets = []
    for r in job.get("rows") or []:
        if phase == "phase1":
            if r.get("status") in ("queued", "running", "paused_budget") or (
                r.get("status") == "failed" and r.get("step", 0) > 0 and r.get("status") == "running"
            ):
                if r.get("status") in ("queued", "running", "paused_budget"):
                    targets.append(r)
        else:
            if r.get("approved_for_phase2") and r.get("status") in (
                "queued",
                "running",
                "paused_budget",
                "ready_for_review",  # after approval, reset below
                "approved",
            ):
                # transition approved review rows into phase2 queued
                if r.get("status") in ("ready_for_review", "approved"):
                    r["status"] = "queued"
                    r["phase"] = "phase2"
                    r["step"] = 0
                targets.append(r)

    if phase == "phase2":
        drive_db.save_bulk_job(job_id, job)

    if concurrency <= 1:
        for r in targets:
            if job_id in _CANCEL_FLAGS or job_id in _PAUSE_FLAGS:
                break
            work(r)
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futs = [ex.submit(work, r) for r in targets]
            for f in as_completed(futs):
                try:
                    f.result()
                except Exception:
                    pass

    job = drive_db.load_bulk_job(job_id)
    if job_id in _CANCEL_FLAGS:
        job["current_phase"] = "cancelled"
    elif job_id in _PAUSE_FLAGS:
        job["current_phase"] = f"{phase}_paused"
    else:
        job["current_phase"] = "phase1_review" if phase == "phase1" else "phase2_review"
    _recompute_totals(job)
    drive_db.save_bulk_job(job_id, job)


def _recompute_totals(job: dict) -> None:
    p1 = {"queued": 0, "done": 0, "failed": 0, "in_progress": 0}
    p2 = {"approved": 0, "done": 0, "failed": 0, "in_progress": 0}
    for r in job.get("rows") or []:
        st = r.get("status") or ""
        if r.get("phase") == "phase2" or r.get("approved_for_phase2"):
            if r.get("approved_for_phase2"):
                p2["approved"] += 1
            if st == "ready":
                p2["done"] += 1
            elif st == "failed":
                p2["failed"] += 1
            elif st in ("running", "queued", "paused_budget"):
                p2["in_progress"] += 1
        if r.get("phase") != "phase2" or not r.get("draft_id"):
            if st == "ready_for_review":
                p1["done"] += 1
            elif st == "failed":
                p1["failed"] += 1
            elif st in ("running",):
                p1["in_progress"] += 1
            elif st == "queued":
                p1["queued"] += 1
    job["totals"] = {"phase1": p1, "phase2": p2}


def resume_inflight_jobs() -> list[str]:
    """On startup: resume any phase1_running / phase2_running jobs."""
    resumed = []
    try:
        for entry in drive_db.list_bulk_jobs(limit=50):
            jid = entry.get("job_id")
            phase = entry.get("current_phase") or ""
            if phase in ("phase1_running", "phase2_running"):
                resumed.append(jid)
                if phase == "phase1_running":
                    threading.Thread(
                        target=run_phase1, args=(jid,), daemon=True, name=f"resume-{jid}"
                    ).start()
                else:
                    threading.Thread(
                        target=run_phase2, args=(jid,), daemon=True, name=f"resume-{jid}"
                    ).start()
    except Exception:
        pass
    return resumed


def start_phase1_async(job_id: str) -> None:
    threading.Thread(target=run_phase1, args=(job_id,), daemon=True, name=f"p1-{job_id}").start()


def start_phase2_async(job_id: str) -> None:
    threading.Thread(target=run_phase2, args=(job_id,), daemon=True, name=f"p2-{job_id}").start()
