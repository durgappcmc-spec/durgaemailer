# NOTE: Drafts inbox — Gmail + Drive drafts, preview, send with open tracking.
from __future__ import annotations

import csv
import io
from typing import Any

import streamlit as st

from config import APP_NAME
from core.auth_ui import logout_button, require_login
from components.draft_inspector import render_draft_inspector

st.set_page_config(page_title=f"Drafts · {APP_NAME}", page_icon="📝", layout="wide")
if not require_login():
    st.stop()
logout_button()

from core import drive_db
from core.tracking import extract_tracking_id, inject_tracking
from gmail_client.drafts import get_gmail_draft, list_gmail_drafts, send_gmail_draft
from gmail_client.send import send_email


def _load_full_draft(draft_id: str, fallback: dict) -> dict:
    try:
        d = drive_db.load_draft(draft_id)
        if d.get("body_html"):
            if not d.get("tracking_id"):
                d["tracking_id"] = extract_tracking_id(d.get("body_html") or "") or ""
            d["has_open_pixel"] = bool(
                d.get("tracking_id")
                or "/.netlify/functions/open" in (d.get("body_html") or "")
            )
            return d
    except Exception:
        pass
    if str(draft_id).startswith("gmail:") or fallback.get("gmail_draft_id"):
        gid = fallback.get("gmail_draft_id") or str(draft_id).removeprefix("gmail:")
        full = get_gmail_draft(gid)
        if not full.get("error"):
            try:
                drive_db.save_draft(full["draft_id"], full)
            except Exception:
                pass
            return full
    return dict(fallback)


def _send_one(draft: dict) -> dict:
    gmail_id = draft.get("gmail_draft_id") or ""
    did = str(draft.get("draft_id") or "")
    if not gmail_id and did.startswith("gmail:"):
        gmail_id = did.removeprefix("gmail:")
    if gmail_id:
        return send_gmail_draft(gmail_id)
    to = draft.get("to") or draft.get("recipient") or ""
    if not to:
        return {"error": "missing recipient", "draft_id": did}
    html, tid = inject_tracking(
        draft.get("body_html") or "",
        tracking_id=draft.get("tracking_id") or None,
        recipient_email=to,
        subject=draft.get("subject") or "",
        register=True,
    )
    result = send_email(
        to=to,
        subject=draft.get("subject") or "(no subject)",
        html_body=html,
        recipient_name=draft.get("recipient_name") or "",
        tracking_id=tid,
        source="drafts_page_send",
        include_signature=False,
    )
    result["draft_id"] = did
    if not result.get("error"):
        try:
            draft["status"] = "sent"
            draft["tracking_id"] = result.get("tracking_id") or tid
            draft["body_html"] = html
            drive_db.save_draft(did, draft)
        except Exception:
            pass
    return result


st.title("📝 Drafts")
st.caption(
    "Review drafts from Chat / Schedule / Bulk · click a subject to open · "
    "send with open-tracking pixel."
)
st.markdown(
    """
<style>
div[data-testid="stHorizontalBlock"] div[data-testid="column"]:nth-child(3) button {
  background: transparent !important;
  border: none !important;
  box-shadow: none !important;
  color: #1a73e8 !important;
  text-align: left !important;
  justify-content: flex-start !important;
  padding-left: 0 !important;
  font-weight: 600 !important;
  text-decoration: underline;
}
div[data-testid="stHorizontalBlock"] div[data-testid="column"]:nth-child(3) button:hover {
  color: #0b57d0 !important;
}
</style>
""",
    unsafe_allow_html=True,
)

with st.spinner("Loading Gmail drafts…"):
    gmail_rows = list_gmail_drafts(limit=50)
gmail_err = next((r for r in gmail_rows if r.get("error")), None)
if gmail_err:
    st.warning(f"Gmail drafts unavailable: {gmail_err.get('error')}")
    gmail_rows = []

try:
    drive_rows = drive_db.list_drafts(limit=5000, offset=0)
except Exception as e:
    st.error(f"Could not load Drive drafts index: {e}")
    drive_rows = []

by_id: dict[str, Any] = {}
for r in drive_rows:
    did = r.get("draft_id")
    if did:
        by_id[did] = {**r, "origin": r.get("source") or "drive"}
for r in gmail_rows:
    did = r.get("draft_id")
    if not did:
        continue
    if did in by_id:
        cur = by_id[did]
        if not cur.get("tracking_id") and r.get("tracking_id"):
            cur["tracking_id"] = r["tracking_id"]
        cur["has_open_pixel"] = cur.get("has_open_pixel") or r.get("has_open_pixel")
        cur["gmail_draft_id"] = r.get("gmail_draft_id") or cur.get("gmail_draft_id")
        cur["origin"] = "drive+gmail"
    else:
        by_id[did] = {**r, "origin": "gmail"}

rows = list(by_id.values())
rows.sort(key=lambda x: str(x.get("updated_at") or ""), reverse=True)

q = st.text_input("Search subject/recipient")
status_f = st.selectbox("Status", ["all", "draft", "ready", "sent", "deleted"])
source_f = st.selectbox("Source", ["all", "gmail", "drive", "bulk"])
page = st.number_input("Page", min_value=1, value=1, step=1)
page_size = 10

if q:
    ql = q.lower()
    rows = [
        r
        for r in rows
        if ql in str(r.get("subject") or "").lower()
        or ql in str(r.get("recipient") or r.get("to") or "").lower()
    ]
if status_f != "all":
    rows = [r for r in rows if (r.get("status") or "draft") == status_f]
if source_f == "gmail":
    rows = [r for r in rows if "gmail" in str(r.get("origin") or "")]
elif source_f == "drive":
    rows = [r for r in rows if "drive" in str(r.get("origin") or "")]
elif source_f == "bulk":
    rows = [r for r in rows if r.get("bulk_job_id")]

total = len(rows)
start = (int(page) - 1) * page_size
page_rows = rows[start : start + page_size]
st.caption(
    f"{total} drafts · showing {start + 1}–{min(start + page_size, total) if total else 0}"
)

h = st.columns([0.4, 2.2, 3.2, 1.3, 1, 1.2, 1.2])
h[0].markdown("**☐**")
h[1].markdown("**Recipient**")
h[2].markdown("**Subject**")
h[3].markdown("**Updated**")
h[4].markdown("**Status**")
h[5].markdown("**Tracking**")
h[6].markdown("**Source**")

selected: list[str] = []
if "opened_draft_id" not in st.session_state:
    st.session_state.opened_draft_id = ""

for r in page_rows:
    did = r.get("draft_id") or ""
    cols = st.columns([0.4, 2.2, 3.2, 1.3, 1, 1.2, 1.2])
    with cols[0]:
        if st.checkbox("", key=f"dsel_{did}", label_visibility="collapsed"):
            selected.append(did)
    cols[1].write(r.get("recipient") or r.get("to") or "—")
    with cols[2]:
        subject_label = (r.get("subject") or "(no subject)").strip() or "(no subject)"
        # Truncate long subjects for the button label
        btn_label = subject_label if len(subject_label) <= 60 else subject_label[:57] + "…"
        if st.button(
            btn_label,
            key=f"open_subj_{did}",
            help="Open this draft",
            use_container_width=True,
        ):
            st.session_state.opened_draft_id = did
            st.rerun()
    cols[3].write((r.get("updated_at") or "")[:16] or "—")
    cols[4].write(r.get("status") or "draft")
    tracked = bool(r.get("tracking_id") or r.get("has_open_pixel"))
    cols[5].write("🔒 yes" if tracked else "⚠️ no")
    cols[6].write(r.get("origin") or r.get("source") or "—")

c1, c2, c3, c4 = st.columns(4)
if c1.button("📨 Send selected", type="primary") and selected:
    results = []
    for did in selected:
        draft = _load_full_draft(did, by_id.get(did) or {})
        results.append(_send_one(draft))
    st.json(results)
if c2.button("🔒 Ensure tracking on selected") and selected:
    for did in selected:
        draft = _load_full_draft(did, by_id.get(did) or {})
        html, tid = inject_tracking(
            draft.get("body_html") or "",
            tracking_id=draft.get("tracking_id") or None,
            recipient_email=draft.get("to") or draft.get("recipient") or "",
            subject=draft.get("subject") or "",
            register=True,
        )
        draft["body_html"] = html
        draft["tracking_id"] = tid
        draft["has_open_pixel"] = True
        try:
            drive_db.save_draft(did, draft)
        except Exception:
            pass
    st.success(f"Tracking injected on {len(selected)} draft(s)")
    st.rerun()
if c3.button("Delete selected") and selected:
    for did in selected:
        try:
            drive_db.delete_draft(did)
        except Exception:
            pass
    st.rerun()
if c4.button("Export CSV"):
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
            "origin",
        ],
    )
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k) for k in w.fieldnames})
    st.download_button("Download", buf.getvalue(), file_name="drafts.csv")

st.divider()
st.subheader("Open a draft")
st.caption("Click a **subject** in the list above to open it.")

opened = st.session_state.get("opened_draft_id") or ""
# Also allow picking from dropdown (synced with click)
labels = {
    r.get("draft_id"): (
        f"{r.get('subject') or '(no subject)'} → "
        f"{r.get('recipient') or r.get('to') or '—'}"
    )
    for r in page_rows
}
options = ["—"] + [r.get("draft_id") for r in page_rows if r.get("draft_id")]
idx = options.index(opened) if opened in options else 0
pick = st.selectbox(
    "Or select here",
    options,
    index=idx,
    format_func=lambda x: "—" if x == "—" else labels.get(x, x),
)
if pick != "—" and pick != opened:
    st.session_state.opened_draft_id = pick
    opened = pick
elif pick == "—" and opened and opened not in (r.get("draft_id") for r in page_rows):
    # keep opened if it's from another page of results
    pass
elif pick == "—" and opened in options:
    # User cleared the selectbox
    st.session_state.opened_draft_id = ""
    opened = ""

if opened:
    c_close, _ = st.columns([1, 5])
    if c_close.button("✕ Close", key="close_opened_draft"):
        st.session_state.opened_draft_id = ""
        st.rerun()
    draft = _load_full_draft(opened, by_id.get(opened) or {})
    render_draft_inspector(draft, key_prefix="drafts_page")
