# NOTE: Shared draft inspector — Intelligence Panel + Agent Trace + tracked pill.
from __future__ import annotations

from typing import Any, Optional

import streamlit as st


def render_draft_inspector(
    draft: dict[str, Any] | None,
    *,
    trace: list[dict] | None = None,
    org_brief: dict | None = None,
    key_prefix: str = "insp",
) -> None:
    if not draft:
        st.info("Select a draft to inspect.")
        return

    tid = draft.get("tracking_id") or ""
    cols = st.columns([3, 1])
    with cols[0]:
        st.subheader(draft.get("subject") or "(no subject)")
        st.caption(f"To: {draft.get('recipient') or draft.get('to') or '—'}")
    with cols[1]:
        if tid:
            st.markdown("🔒 **tracked**")
            st.code(tid[:18] + "…", language=None)
        else:
            st.warning("untracked")

    tab_intel, tab_trace, tab_edit = st.tabs(["Intelligence", "Agent Trace", "Edit"])

    with tab_intel:
        brief = org_brief or draft.get("org_brief") or {}
        if brief:
            st.markdown(f"**Org:** {brief.get('org_name') or '—'}")
            st.markdown(f"**Mission:** {brief.get('mission') or '—'}")
            programs = brief.get("flagship_programs") or []
            if programs:
                st.markdown("**Programs**")
                for p in programs[:8]:
                    if isinstance(p, dict):
                        st.write(f"- {p.get('name')}: {p.get('summary')}")
                    else:
                        st.write(f"- {p}")
            signals = brief.get("recent_signals") or []
            if signals:
                st.markdown("**Recent signals**")
                for s in signals[:5]:
                    if isinstance(s, dict):
                        st.write(f"- {s.get('title')}: {s.get('summary')}")
                    else:
                        st.write(f"- {s}")
        ledger = draft.get("personalization_ledger") or []
        if ledger:
            st.markdown("**Personalization ledger**")
            for i, entry in enumerate(ledger):
                if not isinstance(entry, dict):
                    st.write(f"- {entry}")
                    continue
                label = entry.get("claim") or ""
                ref = entry.get("evidence_ref") or entry.get("source_tool") or ""
                if st.button(f"Ledger {i+1}: {label[:60]}", key=f"{key_prefix}_led_{i}"):
                    st.session_state[f"{key_prefix}_jump_tool"] = entry.get("source_tool")
                    st.info(f"Evidence: {ref}")
        conf = draft.get("confidence")
        if conf is not None:
            st.metric("Confidence", f"{float(conf):.0%}" if float(conf) <= 1 else str(conf))

    with tab_trace:
        events = trace or []
        if not events:
            sid = draft.get("phase2_session_id") or draft.get("phase1_session_id")
            if sid:
                try:
                    from core import drive_db

                    events = drive_db.load_trace(sid)
                except Exception:
                    events = []
        if not events:
            st.caption("No agent trace yet.")
        else:
            for ev in events:
                et = ev.get("type") or "event"
                with st.expander(f"#{ev.get('seq', '?')} {et}", expanded=False):
                    st.json(ev)

    with tab_edit:
        subject = st.text_input(
            "Subject",
            value=draft.get("subject") or "",
            key=f"{key_prefix}_subj",
        )
        body = st.text_area(
            "HTML body",
            value=draft.get("body_html") or draft.get("html") or "",
            height=280,
            key=f"{key_prefix}_body",
        )
        if st.button("Save (re-inject tracking)", key=f"{key_prefix}_save"):
            from core.tracking import inject_tracking
            from core import drive_db

            html, new_tid = inject_tracking(
                body,
                tracking_id=tid or None,
                recipient_email=draft.get("to") or draft.get("recipient") or "",
                subject=subject,
                register=False,
            )
            draft["subject"] = subject
            draft["body_html"] = html
            draft["tracking_id"] = new_tid
            did = draft.get("draft_id")
            if did:
                drive_db.save_draft(did, draft)
                st.success(f"Saved · tracking_id preserved ({new_tid[:8]}…)")
            else:
                st.warning("No draft_id — local only")
            st.markdown(html, unsafe_allow_html=True)
