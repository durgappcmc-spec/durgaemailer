# NOTE: Drafts inbox — Gmail + Drive drafts, preview, send with open tracking.
from __future__ import annotations

import csv
import hashlib
import html as _html_esc
import io
from typing import Any

import streamlit as st

from config import APP_NAME
from core.auth_ui import logout_button, require_login
from components.draft_inspector import enable_editor_mouse_scroll, render_draft_inspector

st.set_page_config(page_title=f"Drafts · {APP_NAME}", page_icon="📝", layout="wide")
if not require_login():
    st.stop()
logout_button()

with st.sidebar:
    try:
        from core.mail_prefs import render_sidebar_signature_pref

        st.subheader("Signature")
        render_sidebar_signature_pref()
    except Exception:
        st.caption("Signature: Gmail account")

from core import drive_db
from core.tracking import extract_tracking_id, inject_tracking
from gmail_client.drafts import (
    delete_gmail_item,
    fetch_gmail_draft,
    fetch_gmail_message,
    gmail_delete_refs,
    gmail_profile_email,
    list_gmail_drafts,
    send_draft,
    _bodies_are_blank,
)
from gmail_client.send import send_email


def _sel_key(did: str) -> str:
    return f"dsel_{did}"


def _on_toggle_draft(did: str) -> None:
    bag = set(st.session_state.get("draft_selected_ids") or [])
    if st.session_state.get(_sel_key(did)):
        bag.add(did)
    else:
        bag.discard(did)
    st.session_state.draft_selected_ids = bag


def _apply_selection(ids: list[str], *, selected: bool) -> None:
    bag = set(st.session_state.get("draft_selected_ids") or [])
    for did in ids:
        if not did:
            continue
        st.session_state[_sel_key(did)] = selected
        if selected:
            bag.add(did)
        else:
            bag.discard(did)
    st.session_state.draft_selected_ids = bag


@st.cache_data(ttl=15)
def _cached_fetch_gmail_draft(draft_id: str) -> dict:
    return fetch_gmail_draft(draft_id)


@st.cache_data(ttl=15)
def _cached_fetch_gmail_message(message_id: str) -> dict:
    return fetch_gmail_message(message_id)


def _md5(text: str) -> str:
    return hashlib.md5((text or "").encode("utf-8")).hexdigest()


def _load_full_draft(draft_id: str, fallback: dict) -> dict:
    """Gmail is the source of truth for preview/edit when a Gmail id exists."""
    gid = ""
    mid = ""
    if str(draft_id).startswith("gmail-msg:") or (
        fallback.get("source") == "gmail_folder" and fallback.get("gmail_message_id")
    ):
        mid = fallback.get("gmail_message_id") or str(draft_id).removeprefix("gmail-msg:")
    if str(draft_id).startswith("gmail:") or fallback.get("gmail_draft_id"):
        gid = fallback.get("gmail_draft_id") or str(draft_id).removeprefix("gmail:")
    if gid:
        fetched = _cached_fetch_gmail_draft(gid)
        if "bcc_cache" not in st.session_state:
            st.session_state.bcc_cache = {}
        gmail_bcc = fetched.get("bcc") or ""
        shown_bcc = gmail_bcc or st.session_state.bcc_cache.get(gid, "")
        body = fetched.get("body") or fetched.get("body_text") or ""
        body_html = fetched.get("body_html") or ""
        to = fetched.get("to") or ""
        if _bodies_are_blank(body_html, body):
            mid_try = (
                fallback.get("gmail_message_id")
                or mid
                or fetched.get("gmail_message_id")
                or ""
            )
            if mid_try:
                fetched_m = _cached_fetch_gmail_message(mid_try)
                m_body = fetched_m.get("body") or fetched_m.get("body_text") or ""
                m_html = fetched_m.get("body_html") or ""
                if not _bodies_are_blank(m_html, m_body):
                    body, body_html = m_body, m_html
                    to = fetched_m.get("to") or to
                    fetched = {**fetched, **fetched_m}
        if _bodies_are_blank(body_html, body):
            snip = (
                fetched.get("snippet")
                or fallback.get("snippet")
                or fallback.get("body")
                or ""
            ).strip()
            if snip:
                body = snip
                body_html = f"<p>{_html_esc.escape(snip)}</p>"
        out = {
            **fallback,
            "draft_id": f"gmail:{gid}",
            "gmail_draft_id": gid,
            "to": to,
            "recipient": to,
            "cc": fetched.get("cc") or "",
            "bcc": gmail_bcc,
            "shown_bcc": shown_bcc,
            "bcc_local": bool(shown_bcc) and not gmail_bcc,
            "subject": fetched.get("subject") or fallback.get("subject") or "",
            "body": body,
            "body_text": body,
            "body_cleaned": body,
            "body_html": body_html,
            "source": "gmail_fetch",
            "gmail_api_status": fetched.get("gmail_api_status"),
            "error": fetched.get("error"),
        }
        return out
    if mid:
        fetched = _cached_fetch_gmail_message(mid)
        body = fetched.get("body") or fetched.get("body_text") or ""
        body_html = fetched.get("body_html") or ""
        to = fetched.get("to") or fallback.get("to") or ""
        if _bodies_are_blank(body_html, body):
            snip = (
                fetched.get("snippet")
                or fallback.get("snippet")
                or fallback.get("body")
                or ""
            ).strip()
            if snip:
                body = snip
                body_html = f"<p>{_html_esc.escape(snip)}</p>"
        return {
            **fallback,
            "draft_id": f"gmail-msg:{mid}",
            "gmail_draft_id": "",
            "gmail_message_id": mid,
            "to": to,
            "recipient": to,
            "cc": fetched.get("cc") or "",
            "bcc": fetched.get("bcc") or "",
            "subject": fetched.get("subject") or fallback.get("subject") or "",
            "body": body,
            "body_text": body,
            "body_cleaned": body,
            "body_html": body_html,
            "source": "gmail_fetch",
            "gmail_api_status": fetched.get("gmail_api_status"),
            "error": fetched.get("error"),
        }
    try:
        d = drive_db.load_draft(draft_id)
        if d.get("body_html") or d.get("body_cleaned") or d.get("body"):
            if not d.get("tracking_id"):
                d["tracking_id"] = extract_tracking_id(d.get("body_html") or "") or ""
            d["has_open_pixel"] = bool(
                d.get("tracking_id")
                or "/.netlify/functions/open" in (d.get("body_html") or "")
            )
            d["source"] = d.get("source") or "session"
            return d
    except Exception:
        pass
    fb = dict(fallback)
    fb["source"] = fb.get("source") or "session"
    return fb


def _send_one(draft: dict) -> dict:
    gmail_id = draft.get("gmail_draft_id") or ""
    did = str(draft.get("draft_id") or "")
    if not gmail_id and did.startswith("gmail:"):
        gmail_id = did.removeprefix("gmail:")
    if gmail_id:
        return send_draft(gmail_id)
    to = draft.get("to") or draft.get("recipient") or ""
    if not to:
        return {"error": "missing recipient", "draft_id": did}
    html, tid = inject_tracking(
        draft.get("body_html") or "",
        tracking_id=draft.get("tracking_id") or None,
        recipient_email=to,
        subject=draft.get("subject") or "",
        register=True,
        track_clicks=True,
        track_opens=True,
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


def _recipient_designation(row: dict) -> str:
    """Job title / designation for the draft list (draft fields or prospect lookup)."""
    title = (
        str(row.get("title") or "").strip()
        or str(row.get("designation") or "").strip()
        or str(row.get("recipient_title") or "").strip()
    )
    if title:
        return title
    email = str(row.get("recipient") or row.get("to") or "").strip().lower()
    if not email or "@" not in email:
        return ""
    try:
        from core.prospect_list import all_prospects

        for p in all_prospects():
            if str(p.get("email") or "").strip().lower() == email:
                return str(p.get("title") or p.get("designation") or "").strip()
    except Exception:
        pass
    return ""


def _recipient_company(row: dict) -> str:
    company = str(row.get("company") or "").strip()
    if company:
        return company
    email = str(row.get("recipient") or row.get("to") or "").strip().lower()
    if not email or "@" not in email:
        return ""
    try:
        from core.prospect_list import all_prospects

        for p in all_prospects():
            if str(p.get("email") or "").strip().lower() == email:
                return str(p.get("company") or "").strip()
    except Exception:
        pass
    return ""


st.title("📝 Drafts")
_profile = ""
try:
    _profile = gmail_profile_email()
except Exception:
    _profile = ""
if _profile:
    chat_acct = st.session_state.get("gmail_profile_email") or ""
    st.session_state["gmail_profile_email_drafts"] = _profile
    if chat_acct and chat_acct.lower() != _profile.lower():
        st.error(
            f"Chat Gmail ({chat_acct}) ≠ Drafts Gmail ({_profile}). "
            "Re-auth so both pages use the same account."
        )
    else:
        st.session_state["gmail_profile_email"] = _profile
    st.caption(f"Gmail account: {_profile}")

with st.expander("Signatures", expanded=False):
    from core.signatures import load_signatures, save_signature

    _sig_user = _profile or st.session_state.get("gmail_profile_email") or ""
    _sigs = load_signatures(_sig_user)
    _sid = st.selectbox(
        "Signature to edit",
        list(_sigs.keys()),
        format_func=lambda k: (_sigs.get(k) or {}).get("name") or k,
        key="drafts_sig_pick",
    )
    if _sid == "none":
        st.caption("None — drafts will have no signature block.")
    elif st.checkbox("Edit this signature", key="drafts_sig_edit_on"):
        try:
            from streamlit_quill import st_quill

            _edited = st_quill(
                value=(_sigs.get(_sid) or {}).get("html") or "",
                html=True,
                toolbar=[["bold", "italic", "underline", "link"], ["clean"]],
                key=f"drafts_sig_quill_{_sid}",
                placeholder="Signature…",
            )
            if _edited is None:
                _edited = (_sigs.get(_sid) or {}).get("html") or ""
        except Exception:
            _edited = st.text_area(
                "Signature HTML",
                value=(_sigs.get(_sid) or {}).get("html") or "",
                height=160,
                key=f"drafts_sig_html_{_sid}",
            )
        if st.button("Save signature", key="drafts_sig_save"):
            save_signature(
                _sig_user,
                _sid,
                name=(_sigs.get(_sid) or {}).get("name") or _sid,
                html=_edited or "",
            )
            st.success("Signature saved — new drafts and the dropdown will use it.")
            st.rerun()
    else:
        st.caption("Check “Edit this signature” to change Default / Short.")
st.caption(
    "Review drafts from Chat / Schedule / Bulk · click a subject to open · "
    "tick one or more checkboxes, then Remove selected · designation shown per recipient."
)
top_a, top_b = st.columns([1, 4])
if top_a.button("🔄 Refresh from Gmail"):
    st.cache_data.clear()
    st.rerun()
st.markdown(
    """
<style>
iframe[title*="quill" i],
iframe[title*="streamlit_quill" i] {
  min-height: 480px !important;
  height: 480px !important;
  max-height: 480px !important;
  overflow: auto !important;
}
div[data-testid="stHtml"] iframe {
  overflow: auto !important;
}
div[data-testid="stHorizontalBlock"] div[data-testid="column"]:nth-child(4) button {
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
div[data-testid="stHorizontalBlock"] div[data-testid="column"]:nth-child(4) button:hover {
  color: #0b57d0 !important;
}
</style>
""",
    unsafe_allow_html=True,
)

# Gmail first (source of truth); Drive only adds designation / tracking metadata
with st.spinner("Loading Gmail drafts…"):
    gmail_rows = list_gmail_drafts(limit=200)
gmail_err = next((r for r in gmail_rows if r.get("error")), None)
if gmail_err:
    st.warning(f"Gmail drafts unavailable: {gmail_err.get('error')}")
    gmail_rows = []
else:
    st.caption(
        f"Synced **{len(gmail_rows)}** Gmail Drafts-folder item(s) for this account. "
        "Click **Refresh from Gmail** if a new draft is missing."
    )

try:
    with st.spinner("Loading Drive draft metadata…"):
        drive_rows = drive_db.list_drafts(limit=5000, offset=0)
except Exception as e:
    st.error(f"Could not load Drive drafts index: {e}")
    drive_rows = []

by_id: dict[str, Any] = {}
for r in gmail_rows:
    did = r.get("draft_id")
    if did:
        by_id[did] = {**r, "origin": "gmail"}
for r in drive_rows:
    did = r.get("draft_id")
    if not did:
        continue
    if did in by_id:
        cur = by_id[did]
        if not cur.get("tracking_id") and r.get("tracking_id"):
            cur["tracking_id"] = r["tracking_id"]
        cur["has_open_pixel"] = cur.get("has_open_pixel") or r.get("has_open_pixel")
        for extra in ("title", "designation", "company", "recipient_name", "bulk_job_id"):
            if not cur.get(extra) and r.get(extra):
                cur[extra] = r[extra]
        cur["origin"] = "drive+gmail"
    else:
        by_id[did] = {**r, "origin": r.get("source") or "drive"}

rows = list(by_id.values())
# Backfill designation/company from Saved prospects when Gmail metadata lacks them
for r in rows:
    if not _recipient_designation(r) or not _recipient_company(r):
        des = _recipient_designation(r)
        co = _recipient_company(r)
        if des and not (r.get("title") or r.get("designation")):
            r["title"] = des
            r["designation"] = des
        if co and not r.get("company"):
            r["company"] = co
rows.sort(key=lambda x: str(x.get("updated_at") or ""), reverse=True)

q = st.text_input("Search subject/recipient/designation")
status_f = st.selectbox("Status", ["active", "all", "draft", "ready", "sent", "deleted"])
source_f = st.selectbox("Source", ["all", "gmail", "drive", "bulk"])
page_size = 20

if q:
    ql = q.lower()
    rows = [
        r
        for r in rows
        if ql in str(r.get("subject") or "").lower()
        or ql in str(r.get("recipient") or r.get("to") or "").lower()
        or ql in str(r.get("snippet") or "").lower()
        or ql in str(r.get("recipient_name") or "").lower()
        or ql in str(r.get("title") or r.get("designation") or "").lower()
        or ql in str(r.get("company") or "").lower()
        or ql in _recipient_designation(r).lower()
        or ql in _recipient_company(r).lower()
    ]
if status_f == "active":
    rows = [
        r
        for r in rows
        if (r.get("status") or "draft") not in ("deleted", "sent")
    ]
elif status_f != "all":
    rows = [r for r in rows if (r.get("status") or "draft") == status_f]
if source_f == "gmail":
    rows = [r for r in rows if "gmail" in str(r.get("origin") or "")]
elif source_f == "drive":
    rows = [r for r in rows if "drive" in str(r.get("origin") or "")]
elif source_f == "bulk":
    rows = [r for r in rows if r.get("bulk_job_id")]

total = len(rows)
if "drafts_page" not in st.session_state:
    st.session_state.drafts_page = 1
filt_key = f"{q}|{status_f}|{source_f}"
if st.session_state.get("drafts_filter_key") != filt_key:
    st.session_state.drafts_filter_key = filt_key
    st.session_state.drafts_page = 1
total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
page = max(1, min(int(st.session_state.drafts_page or 1), total_pages))
st.session_state.drafts_page = page
start = (page - 1) * page_size
page_rows = rows[start : start + page_size]
shown_end = min(start + page_size, total) if total else 0
cap_l, cap_r = st.columns([3, 1])
cap_l.caption(
    f"{total} drafts · showing {start + 1 if total else 0}–{shown_end} · page {page} of {total_pages}"
)
if cap_r.button("Next →", disabled=page >= total_pages or not total, key="drafts_next_top"):
    st.session_state.drafts_page = page + 1
    st.rerun()

if "draft_selected_ids" not in st.session_state:
    st.session_state.draft_selected_ids = set()
if "opened_draft_id" not in st.session_state:
    st.session_state.opened_draft_id = ""


def _remove_drafts(ids: list[str]) -> list[dict]:
    results = []
    for did in ids:
        row = by_id.get(did) or {}
        gmail_id, mid = gmail_delete_refs(did, row)
        gmail_res: dict = {}
        if gmail_id or mid:
            gmail_res = delete_gmail_item(
                gmail_draft_id=gmail_id, gmail_message_id=mid
            )
        try:
            drive_db.delete_draft(did, purge=True)
            drive_ok = True
        except Exception as e:
            drive_ok = False
            gmail_res = {**gmail_res, "drive_error": str(e)}
        bag = set(st.session_state.get("draft_selected_ids") or [])
        bag.discard(did)
        st.session_state.draft_selected_ids = bag
        st.session_state.pop(_sel_key(did), None)
        if st.session_state.get("opened_draft_id") == did:
            st.session_state.opened_draft_id = ""
        results.append(
            {
                "draft_id": did,
                "gmail": gmail_res,
                "drive_removed": drive_ok,
                "ok": bool(drive_ok or (gmail_res or {}).get("ok")),
            }
        )
    return results


page_ids = [str(r.get("draft_id") or "") for r in page_rows if r.get("draft_id")]
all_ids = [str(r.get("draft_id") or "") for r in rows if r.get("draft_id")]
n_sel = len(st.session_state.draft_selected_ids)

sel_c1, sel_c2, sel_c3, sel_c4 = st.columns([1.15, 1.35, 1.15, 2.3])
if sel_c1.button("☑ Select page", help="Select every draft on this page"):
    _apply_selection(page_ids, selected=True)
    st.rerun()
if sel_c2.button(
    f"☑ Select all {total}" if total else "☑ Select all",
    help="Select every draft matching the current filters",
    disabled=not all_ids,
):
    _apply_selection(all_ids, selected=True)
    st.rerun()
if sel_c3.button("☐ Clear selection"):
    _apply_selection(list(st.session_state.draft_selected_ids) + page_ids, selected=False)
    st.session_state.draft_selected_ids = set()
    st.rerun()
sel_c4.caption(f"**{n_sel}** selected" + (f" of {total}" if total else ""))

a1, a2, a3, a4 = st.columns(4)
send_clicked = a1.button("📨 Send selected", type="primary")
track_clicked = a2.button("🔒 Ensure tracking on selected")
remove_clicked = a3.button("🗑 Remove selected")
export_clicked = a4.button("Export CSV")
action_ids = list(st.session_state.draft_selected_ids)

if send_clicked:
    if not action_ids:
        st.warning("Select one or more drafts first.")
    else:
        results = []
        for did in action_ids:
            draft = _load_full_draft(did, by_id.get(did) or {})
            results.append(_send_one(draft))
        st.json(results)
if track_clicked:
    if not action_ids:
        st.warning("Select one or more drafts first.")
    else:
        from core.tracking import prepare_draft_tracking

        for did in action_ids:
            draft = _load_full_draft(did, by_id.get(did) or {})
            html, tid = prepare_draft_tracking(
                draft.get("body_html") or "",
                draft.get("tracking_id") or None,
            )
            draft["body_html"] = html
            draft["tracking_id"] = tid
            draft["has_open_pixel"] = False
            try:
                drive_db.save_draft(did, draft)
            except Exception:
                pass
            gid = draft.get("gmail_draft_id") or (
                str(did).removeprefix("gmail:") if str(did).startswith("gmail:") else ""
            )
            if gid:
                try:
                    from gmail_client.drafts import save_gmail_draft

                    save_gmail_draft(
                        gid,
                        draft.get("to") or "",
                        draft.get("cc") or "",
                        draft.get("bcc") or "",
                        draft.get("subject") or "",
                        html,
                        from_email=draft.get("from") or None,
                    )
                except Exception:
                    pass
        st.success(
            f"Tracking id saved on {len(action_ids)} draft(s) — pixel is added at send."
        )
        st.rerun()
if remove_clicked:
    if not action_ids:
        st.warning("Select one or more drafts first.")
    else:
        results = _remove_drafts(action_ids)
        ok_n = sum(1 for r in results if r.get("ok"))
        gmail_n = sum(1 for r in results if (r.get("gmail") or {}).get("ok"))
        st.cache_data.clear()
        st.success(
            f"Removed {ok_n}/{len(results)} draft(s)"
            + (f" ({gmail_n} from Gmail)" if gmail_n else "")
        )
        st.rerun()
if export_clicked:
    buf = io.StringIO()
    w = csv.DictWriter(
        buf,
        fieldnames=[
            "draft_id",
            "recipient",
            "cc",
            "recipient_name",
            "title",
            "designation",
            "company",
            "subject",
            "status",
            "updated_at",
            "tracking_id",
            "origin",
        ],
    )
    w.writeheader()
    for r in rows:
        row = {k: r.get(k) for k in w.fieldnames}
        if not row.get("title") and not row.get("designation"):
            row["title"] = _recipient_designation(r)
            row["designation"] = row["title"]
        if not row.get("company"):
            row["company"] = _recipient_company(r)
        w.writerow(row)
    st.download_button("Download", buf.getvalue(), file_name="drafts.csv")

h = st.columns([0.45, 1.8, 1.4, 2.2, 1.6, 1.1, 0.8, 0.9])
h[0].markdown("**☐**")
h[1].markdown("**Recipient**")
h[2].markdown("**Designation**")
h[3].markdown("**Subject**")
h[4].markdown("**Cc**")
h[5].markdown("**Updated**")
h[6].markdown("**Status**")
h[7].markdown("**Source**")

for r in page_rows:
    did = str(r.get("draft_id") or "")
    cols = st.columns([0.45, 1.8, 1.4, 2.2, 1.6, 1.1, 0.8, 0.9])
    with cols[0]:
        ck = _sel_key(did)
        if ck not in st.session_state:
            st.session_state[ck] = did in st.session_state.draft_selected_ids
        st.checkbox(
            "select",
            key=ck,
            on_change=_on_toggle_draft,
            args=(did,),
            label_visibility="collapsed",
        )
    with cols[1]:
        email = r.get("recipient") or r.get("to") or "—"
        name = (r.get("recipient_name") or "").strip()
        company = _recipient_company(r)
        st.write(email)
        if name and name.lower() not in str(email).lower():
            st.caption(name)
        elif company:
            st.caption(company)
    with cols[2]:
        designation = _recipient_designation(r)
        company = _recipient_company(r)
        if designation:
            st.write(designation)
            if company:
                st.caption(company)
        else:
            st.caption(company or "—")
    with cols[3]:
        subject_label = (r.get("subject") or "(no subject)").strip() or "(no subject)"
        btn_label = subject_label if len(subject_label) <= 52 else subject_label[:49] + "…"
        if st.button(
            btn_label,
            key=f"open_subj_{did}",
            help="Open this draft",
            use_container_width=True,
        ):
            st.session_state.opened_draft_id = did
            st.rerun()
    with cols[4]:
        cc_full = str(r.get("cc") or "").strip()
        if not cc_full:
            st.caption("—")
        else:
            shown = cc_full if len(cc_full) <= 40 else cc_full[:37] + "…"
            st.markdown(
                f'<span title="{_html_esc.escape(cc_full)}">{_html_esc.escape(shown)}</span>',
                unsafe_allow_html=True,
            )
    cols[5].write((r.get("updated_at") or "")[:16] or "—")
    cols[6].write(r.get("status") or "draft")
    cols[7].write(r.get("origin") or r.get("source") or "—")

nav_l, nav_m, nav_r = st.columns([1, 2, 1])
if nav_l.button("← Previous", disabled=page <= 1, key="drafts_prev_bottom"):
    st.session_state.drafts_page = page - 1
    st.rerun()
nav_m.markdown(
    f"<p style='text-align:center;margin:0.45rem 0 0 0'>Page {page} of {total_pages}</p>",
    unsafe_allow_html=True,
)
if nav_r.button(
    "Next →",
    type="primary",
    disabled=page >= total_pages or not total,
    key="drafts_next_bottom",
):
    st.session_state.drafts_page = page + 1
    st.rerun()

st.divider()
st.subheader("Open a draft")
st.caption("Click a **subject** in the list above to open it.")

opened = st.session_state.get("opened_draft_id") or ""
# Also allow picking from dropdown (synced with click)
labels = {
    r.get("draft_id"): (
        f"{r.get('subject') or '(no subject)'} → "
        f"{r.get('recipient') or r.get('to') or '—'}"
        + (
            f" · {_recipient_designation(r)}"
            if _recipient_designation(r)
            else ""
        )
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
    gid = draft.get("gmail_draft_id") or ""
    preview_body = draft.get("body_html") or draft.get("body") or ""
    edit_body = preview_body
    if gid:
        for k, v in st.session_state.items():
            if str(k).startswith(f"quill_{gid}"):
                edit_body = v or edit_body
    with st.expander("Debug · Preview / Edit / Gmail hashes", expanded=False):
        st.write(f"draft_id: `{draft.get('draft_id') or opened}`")
        st.write(f"source: `{draft.get('source') or 'session'}`")
        st.write(f"Gmail API status: `{draft.get('gmail_api_status') or draft.get('error') or '—'}`")
        if draft.get("error"):
            st.error(draft["error"])
        st.write(f"md5(body_html_in_preview): `{_md5(preview_body)}`")
        st.write(f"md5(body_in_quill): `{_md5(str(edit_body or ''))}`")
        st.write(f"md5(body_returned_by_gmail): `{_md5(preview_body if gid else '')}`")
        st.write(f"Cc header: `{draft.get('cc') or ''}`")
        st.write(f"Bcc header: `{draft.get('bcc') or ''}`")
        if draft.get("bcc_local"):
            st.caption(f"Bcc (local cache): {draft.get('shown_bcc') or ''}")
        gmail_hash = _md5(preview_body) if gid else ""
        if gid and _md5(preview_body) == _md5(str(edit_body or "")) == gmail_hash:
            st.success("Preview / Edit / Gmail body hashes match")
        elif gid:
            st.warning(
                "Body hashes differ — Edit may have unsaved changes, "
                "or the editor has not mounted yet."
            )
    render_draft_inspector(draft, key_prefix="drafts_page")
