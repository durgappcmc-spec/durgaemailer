# NOTE: Drive-backed drafts list with pagination + shared inspector.
from __future__ import annotations

import csv
import io

import streamlit as st

from config import APP_NAME
from core.auth_ui import logout_button, require_login
from components.draft_inspector import render_draft_inspector

st.set_page_config(page_title=f"Drafts · {APP_NAME}", page_icon="📝", layout="wide")
if not require_login():
    st.stop()
logout_button()

from core import drive_db
from gmail_client.send import send_bulk_serial

st.title("📝 Drafts")
st.caption("Drive-backed drafts · server-side pagination")

q = st.text_input("Search subject/recipient")
status_f = st.selectbox("Status", ["all", "ready", "draft", "sent", "deleted"])
job_f = st.text_input("Bulk job id filter")
page = st.number_input("Page", min_value=1, value=1, step=1)
page_size = 10

try:
    all_idx = drive_db.list_drafts(limit=5000, offset=0)
except Exception as e:
    st.error(f"Could not load drafts index: {e}")
    st.stop()

rows = all_idx
if q:
    ql = q.lower()
    rows = [
        r
        for r in rows
        if ql in str(r.get("subject") or "").lower()
        or ql in str(r.get("recipient") or "").lower()
    ]
if status_f != "all":
    rows = [r for r in rows if (r.get("status") or "") == status_f]
if job_f.strip():
    rows = [r for r in rows if (r.get("bulk_job_id") or "") == job_f.strip()]

total = len(rows)
start = (int(page) - 1) * page_size
page_rows = rows[start : start + page_size]
st.caption(f"{total} drafts · showing {start+1}-{min(start+page_size, total)}")

selected: list[str] = []
for r in page_rows:
    cols = st.columns([0.4, 2, 3, 1.2, 1, 0.8, 0.8, 1.2, 1.2])
    with cols[0]:
        if st.checkbox("", key=f"dsel_{r.get('draft_id')}", label_visibility="collapsed"):
            selected.append(r["draft_id"])
    cols[1].write(r.get("recipient") or "—")
    cols[2].write(r.get("subject") or "—")
    cols[3].write((r.get("updated_at") or "")[:16])
    cols[4].write(r.get("status") or "")
    cols[5].write(r.get("opens") or 0)
    cols[6].write(r.get("clicks") or 0)
    cols[7].write(r.get("bulk_job_id") or "—")
    cols[8].write(r.get("confidence") or "—")

c1, c2, c3, c4 = st.columns(4)
if c1.button("Send selected") and selected:
    jobs = []
    for did in selected:
        d = drive_db.load_draft(did)
        jobs.append(
            {
                "draft_id": did,
                "to": d.get("to") or d.get("recipient"),
                "subject": d.get("subject"),
                "body_html": d.get("body_html"),
                "tracking_id": d.get("tracking_id"),
            }
        )
    st.json(send_bulk_serial(jobs))
if c2.button("Delete selected") and selected:
    for did in selected:
        drive_db.delete_draft(did)
    st.rerun()
if c3.button("Export CSV"):
    buf = io.StringIO()
    w = csv.DictWriter(
        buf,
        fieldnames=[
            "draft_id",
            "recipient",
            "subject",
            "status",
            "updated_at",
            "tracking_id",
            "bulk_job_id",
        ],
    )
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k) for k in w.fieldnames})
    st.download_button("Download", buf.getvalue(), file_name="drafts.csv")

st.divider()
pick = st.selectbox(
    "Inspect draft",
    ["—"] + [r.get("draft_id") for r in page_rows],
)
if pick and pick != "—":
    draft = drive_db.load_draft(pick)
    render_draft_inspector(draft, key_prefix="drafts_page")
