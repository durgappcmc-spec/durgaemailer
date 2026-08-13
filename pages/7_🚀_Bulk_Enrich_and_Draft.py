# NOTE: Two-phase Bulk Enrich & Draft UI (Phase 1 + human gate + Phase 2).
from __future__ import annotations

import csv
import io
import re

import streamlit as st

from config import APP_NAME
from core.auth_ui import logout_button, require_login

st.set_page_config(
    page_title=f"Bulk Enrich · {APP_NAME}",
    page_icon="🚀",
    layout="wide",
)
if not require_login():
    st.stop()
logout_button()

try:
    from streamlit_autorefresh import st_autorefresh
except Exception:
    st_autorefresh = None

from core import bulk_pipeline, drive_db
from components.draft_inspector import render_draft_inspector


@st.cache_resource
def _resume_bulk_jobs():
    try:
        return bulk_pipeline.resume_inflight_jobs()
    except Exception:
        return []


_resume_bulk_jobs()

st.title("🚀 Bulk Enrich & Draft")
st.caption("Phase 1 contact enrichment → human review → Phase 2 hyper-personalized drafts. Single Gemini model from .env.")

# Sidebar meters
with st.sidebar:
    st.subheader("Usage (MTD)")
    try:
        gm = drive_db.gemini_usage_mtd()
        st.metric("Gemini calls", gm.get("totals", {}).get("calls", 0))
        st.caption(
            f"tokens in/out: {gm.get('totals', {}).get('tokens_in', 0)} / {gm.get('totals', {}).get('tokens_out', 0)}"
        )
        for kind, b in (gm.get("by_task_kind") or {}).items():
            st.caption(f"{kind}: {b.get('calls', 0)} calls")
    except Exception as e:
        st.caption(f"Gemini usage unavailable: {e}")
    try:
        zi = drive_db.zoominfo_usage_mtd()
        st.metric("ZI credits (MTD)", zi.get("credits", 0))
    except Exception:
        pass
    try:
        from connectors.zoominfo import ZoomInfoConnector

        hc = ZoomInfoConnector().health_check()
        st.markdown("🟢 ZoomInfo" if hc.get("ok") else f"🔴 ZoomInfo — {hc.get('detail')}")
    except Exception as e:
        st.markdown(f"🔴 ZoomInfo — {e}")

# Job selection
jobs = []
try:
    jobs = drive_db.list_bulk_jobs(limit=30)
except Exception:
    pass

job_id = st.session_state.get("bulk_job_id")
cols = st.columns([2, 1, 1])
with cols[0]:
    options = ["— new job —"] + [j.get("job_id") for j in jobs]
    pick = st.selectbox("Active job", options, index=options.index(job_id) if job_id in options else 0)
    if pick != "— new job —":
        job_id = pick
        st.session_state.bulk_job_id = job_id

job = None
if job_id:
    try:
        job = drive_db.load_bulk_job(job_id)
    except Exception:
        job = None

# ── Step 1: Phase 1 config ────────────────────────────────────────────────
st.header("Step 1 — Phase 1 enrichment")
persona_presets = []
try:
    persona_presets = drive_db.load_persona_targets()
except Exception:
    persona_presets = []
if not persona_presets:
    persona_presets = [
        {
            "id": "csr_head",
            "label": "CSR Head",
            "titles": [
                "CSR Head",
                "Head of CSR",
                "CSR Manager",
                "Head of Partnerships",
                "Director of Partnerships",
            ],
            "seniority": ["Director", "VP", "C-Level", "Manager"],
        }
    ]

labels = [p.get("label") or p.get("id") for p in persona_presets]
p_idx = st.selectbox("Persona target", range(len(labels)), format_func=lambda i: labels[i])
persona = persona_presets[p_idx]

companies_text = st.text_area(
    "Company names (one per line)",
    height=140,
    placeholder="Pratham\nTeach for India\nMagic Bus",
    key="bulk_companies",
)
uploaded = st.file_uploader("Or CSV with a company column", type=["csv"])
zi_budget = st.slider("ZI credit budget", 10, 500, 100)
gem_budget = st.slider("Gemini token budget (Phase 1)", 20000, 1000000, 200000, step=10000)
concurrency = st.selectbox("Concurrency", [1, 2, 3], index=0)

if st.button("Start Enrichment (Phase 1)", type="primary"):
    names: list[str] = []
    if companies_text.strip():
        names.extend([ln.strip() for ln in companies_text.splitlines() if ln.strip()])
    if uploaded is not None:
        text = uploaded.getvalue().decode("utf-8", errors="ignore")
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames:
            col = next(
                (
                    c
                    for c in reader.fieldnames
                    if c.lower() in ("company", "org", "organization", "name", "input")
                ),
                reader.fieldnames[0],
            )
            for row in reader:
                v = (row.get(col) or "").strip()
                if v:
                    names.append(v)
        else:
            for ln in text.splitlines():
                if ln.strip():
                    names.append(ln.strip())
    names = list(dict.fromkeys(names))
    if not names:
        st.error("Add at least one company name")
    else:
        jid = bulk_pipeline.create_enrichment_job(
            names,
            persona_target=persona,
            zi_credit_budget=zi_budget,
            gemini_token_budget=gem_budget,
            concurrency=concurrency,
        )
        st.session_state.bulk_job_id = jid
        bulk_pipeline.start_phase1_async(jid)
        st.success(f"Started Phase 1 · job `{jid}` · {len(names)} rows")
        st.rerun()

# ── Step 2: Review grid ───────────────────────────────────────────────────
if job:
    st.header("Step 2 — Phase 1 review")
    st.caption(f"Job `{job_id}` · phase **{job.get('current_phase')}**")
    if st_autorefresh and str(job.get("current_phase") or "").endswith("_running"):
        st_autorefresh(interval=3000, key="bulk_refresh")

    t1 = (job.get("totals") or {}).get("phase1") or {}
    cfg = job.get("config") or {}
    st.progress(
        min(
            1.0,
            (t1.get("done", 0) + t1.get("failed", 0))
            / max(1, t1.get("done", 0) + t1.get("failed", 0) + t1.get("queued", 0) + t1.get("in_progress", 0)),
        ),
        text=f"{t1.get('done', 0)} ready · {t1.get('failed', 0)} failed · {t1.get('in_progress', 0)} running",
    )
    m1, m2, m3 = st.columns(3)
    m1.metric("ZI credits", f"{cfg.get('zi_credits_used', 0)}/{cfg.get('zi_credit_budget', 0)}")
    used_tok = sum(int(r.get("gemini_tokens_used") or 0) for r in (job.get("rows") or []))
    m2.metric("Gemini tokens", f"{used_tok}/{cfg.get('gemini_token_budget', 0)}")
    m3.metric("Rows", len(job.get("rows") or []))

    b1, b2, b3, b4, b5 = st.columns(5)
    if b1.button("Pause"):
        bulk_pipeline.pause_job(job_id)
        st.rerun()
    if b2.button("Resume"):
        bulk_pipeline.resume_job(job_id)
        st.rerun()
    if b3.button("Retry failed"):
        bulk_pipeline.retry_failed_rows(job_id, "phase1")
        st.rerun()
    if b4.button("Cancel"):
        bulk_pipeline.cancel_job(job_id)
        st.rerun()
    if b5.button("Export CSV"):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(
            [
                "row_id",
                "input",
                "org",
                "domain",
                "name",
                "title",
                "email",
                "mobile",
                "linkedin",
                "matched_on",
                "industry",
                "hq",
                "employees",
                "status",
            ]
        )
        for r in job.get("rows") or []:
            c = r.get("contact") or {}
            s = r.get("light_org_signal") or {}
            w.writerow(
                [
                    r.get("row_id"),
                    r.get("input"),
                    r.get("resolved_org_name"),
                    r.get("resolved_domain"),
                    c.get("name"),
                    c.get("title"),
                    c.get("email"),
                    c.get("mobile"),
                    c.get("linkedin_url"),
                    c.get("matched_on"),
                    s.get("industry"),
                    s.get("hq"),
                    s.get("employee_band"),
                    r.get("status"),
                ]
            )
        st.download_button("Download CSV", buf.getvalue(), file_name=f"{job_id}.csv")

    filt = st.selectbox(
        "Filter",
        ["all", "ready_for_review", "failed", "in-progress", "approved"],
    )
    rows = job.get("rows") or []
    if filt == "ready_for_review":
        rows = [r for r in rows if r.get("status") == "ready_for_review"]
    elif filt == "failed":
        rows = [r for r in rows if r.get("status") == "failed"]
    elif filt == "in-progress":
        rows = [r for r in rows if r.get("status") in ("running", "queued", "paused_budget")]
    elif filt == "approved":
        rows = [r for r in rows if r.get("approved_for_phase2")]

    status_icon = {
        "queued": "🟡",
        "running": "🔵",
        "ready_for_review": "🟢",
        "failed": "🔴",
        "skipped": "⚪",
        "approved": "🟣",
        "ready": "🟢",
        "paused_budget": "🟠",
        "awaiting_human": "🟣",
    }

    selected: list[str] = []
    for r in rows:
        c = r.get("contact") or {}
        s = r.get("light_org_signal") or {}
        icon = status_icon.get(r.get("status"), "⚪")
        recovery = f" 🔁{r.get('recovery_count')}" if r.get("recovery_count") else ""
        with st.expander(
            f"{icon} {r.get('row_id')} · {r.get('input')} · {c.get('name') or '—'} · {r.get('status')}{recovery}",
            expanded=False,
        ):
            cols = st.columns([1, 3, 1])
            with cols[0]:
                if st.checkbox("Select", key=f"sel_{r.get('row_id')}"):
                    selected.append(r["row_id"])
            with cols[1]:
                st.write(
                    {
                        "org": r.get("resolved_org_name"),
                        "domain": r.get("resolved_domain"),
                        "title": c.get("title"),
                        "email": c.get("email"),
                        "mobile": c.get("mobile"),
                        "linkedin": c.get("linkedin_url"),
                        "matched_on": c.get("matched_on"),
                        "industry": s.get("industry"),
                        "hq": s.get("hq"),
                        "employees": s.get("employee_band"),
                        "reason": r.get("status_message"),
                    }
                )
            with cols[2]:
                if st.button("Skip", key=f"skip_{r['row_id']}"):
                    drive_db.update_bulk_row(job_id, r["row_id"], {"status": "skipped"})
                    st.rerun()
            st.markdown("**🧠 Agent trace**")
            sid = r.get("phase1_session_id") or r.get("phase2_session_id")
            if sid:
                try:
                    for ev in drive_db.tail_trace(sid, 0)[-12:]:
                        st.caption(
                            f"#{ev.get('seq')} {ev.get('type')} "
                            f"{(ev.get('tool') or '')} {(ev.get('result') or {}).get('ok', '')}"
                        )
                except Exception as e:
                    st.caption(str(e))
            with st.form(f"override_{r['row_id']}"):
                st.write("Manual override")
                on = st.text_input("Name", value=c.get("name") or "")
                oem = st.text_input("Email", value=c.get("email") or "")
                omob = st.text_input("Mobile", value=c.get("mobile") or "")
                otit = st.text_input("Title", value=c.get("title") or "")
                if st.form_submit_button("Apply override → ready_for_review"):
                    contact = {
                        "name": on,
                        "email": oem,
                        "mobile": omob,
                        "title": otit,
                        "linkedin_url": c.get("linkedin_url") or "",
                        "matched_on": "manual_override",
                        "confidence": 1.0,
                    }
                    drive_db.update_bulk_row(
                        job_id,
                        r["row_id"],
                        {
                            "contact": contact,
                            "status": "ready_for_review",
                            "status_message": "manual_override",
                        },
                    )
                    st.rerun()

    st.session_state.bulk_selected = selected
    a1, a2, a3 = st.columns(3)
    if a1.button("Approve selected for drafting"):
        ids = st.session_state.get("bulk_selected") or []
        if not ids:
            st.warning("Select rows first")
        else:
            for rid in ids:
                drive_db.update_bulk_row(
                    job_id, rid, {"approved_for_phase2": True, "status": "approved"}
                )
            st.success(f"Approved {len(ids)} rows")
            st.rerun()
    if a2.button("Approve all ready_for_review"):
        for r in job.get("rows") or []:
            if r.get("status") == "ready_for_review":
                drive_db.update_bulk_row(
                    job_id,
                    r["row_id"],
                    {"approved_for_phase2": True, "status": "approved"},
                )
        st.rerun()

    # refresh job
    job = drive_db.load_bulk_job(job_id)
    approved = [r for r in (job.get("rows") or []) if r.get("approved_for_phase2")]

    # ── Step 3: Phase 2 config ─────────────────────────────────────────────
    if approved:
        st.header("Step 3 — Phase 2 drafting config")
        st.metric("Approved rows", len(approved))
        intent = st.selectbox(
            "Intent",
            ["partnership_outreach", "follow_up", "intro", "event_invite"],
        )
        ref_q = st.text_input("Reference sent email search (e.g. magicbus)", value="")
        instructions = st.text_area("Instructions", height=100)
        track_on = st.checkbox("Tracking ON", value=True)
        p2_budget = st.slider(
            "Phase 2 Gemini token budget",
            10000,
            2000000,
            max(5000 * len(approved), 50000),
            step=5000,
        )
        if st.button("Start Drafting (Phase 2)", type="primary"):
            source_email = None
            if ref_q:
                try:
                    from core.sent_items import find_similar_sent

                    hits = find_similar_sent(reference_query=ref_q, limit=1)
                    source_email = hits[0] if hits else {"query": ref_q}
                except Exception:
                    source_email = {"query": ref_q}
            style = {}
            try:
                style = drive_db.load_style_profile()
            except Exception:
                pass
            cfg = {
                "intent": intent,
                "instructions": instructions,
                "tracking": track_on,
                "source_email": source_email,
                "style_profile": style,
                "gemini_token_budget": p2_budget,
            }
            bulk_pipeline.approve_rows_for_phase2(
                job_id,
                [r["row_id"] for r in approved],
                cfg,
            )
            # bump job gemini budget
            j = drive_db.load_bulk_job(job_id)
            j["config"]["gemini_token_budget"] = int(
                j["config"].get("gemini_token_budget") or 0
            ) + int(p2_budget)
            drive_db.save_bulk_job(job_id, j)
            bulk_pipeline.start_phase2_async(job_id)
            st.success(f"Phase 2 started for {len(approved)} rows")
            st.rerun()

    # ── Step 4: Phase 2 grid ───────────────────────────────────────────────
    st.header("Step 4 — Drafts")
    for r in job.get("rows") or []:
        if not (r.get("draft_id") or r.get("phase") == "phase2" or r.get("approved_for_phase2")):
            continue
        icon = status_icon.get(r.get("status"), "🟠")
        c = r.get("contact") or {}
        with st.expander(
            f"{icon} {r.get('row_id')} · {c.get('name')} · draft={r.get('draft_id') or '—'} · {r.get('status')}"
        ):
            draft = None
            if r.get("draft_id"):
                try:
                    draft = drive_db.load_draft(r["draft_id"])
                except Exception:
                    draft = r.get("draft")
            else:
                draft = r.get("draft")
            render_draft_inspector(
                draft,
                org_brief=r.get("org_brief"),
                key_prefix=f"p2_{r.get('row_id')}",
            )

    # Bulk send
    ready_drafts = [
        r for r in (job.get("rows") or []) if r.get("status") == "ready" and r.get("draft_id")
    ]
    if ready_drafts and st.button(f"Send {len(ready_drafts)} ready drafts now"):
        from gmail_client.send import send_bulk_serial

        jobs_payload = []
        for r in ready_drafts:
            d = drive_db.load_draft(r["draft_id"])
            jobs_payload.append(
                {
                    "draft_id": d.get("draft_id"),
                    "to": d.get("to") or d.get("recipient"),
                    "subject": d.get("subject"),
                    "body_html": d.get("body_html"),
                    "tracking_id": d.get("tracking_id"),
                    "recipient_name": d.get("recipient_name"),
                }
            )
        results = send_bulk_serial(jobs_payload)
        st.json(results)
