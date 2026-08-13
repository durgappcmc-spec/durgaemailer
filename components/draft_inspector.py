# NOTE: Shared draft inspector — preview, tracking pill, edit, send now.
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
    has_pixel = bool(draft.get("has_open_pixel")) or (
        "/.netlify/functions/open" in (draft.get("body_html") or "")
        or "/t/o/" in (draft.get("body_html") or "")
    )
    if not tid and has_pixel:
        try:
            from core.tracking import extract_tracking_id

            tid = extract_tracking_id(draft.get("body_html") or "") or ""
            draft["tracking_id"] = tid
        except Exception:
            pass

    cols = st.columns([3, 1])
    with cols[0]:
        st.subheader(draft.get("subject") or "(no subject)")
        to_line = draft.get("recipient") or draft.get("to") or "—"
        name = (draft.get("recipient_name") or "").strip()
        title = (
            str(draft.get("title") or "").strip()
            or str(draft.get("designation") or "").strip()
            or str(draft.get("recipient_title") or "").strip()
        )
        company = str(draft.get("company") or "").strip()
        if not title or not company:
            try:
                from core.prospect_list import all_prospects

                email = str(to_line).strip().lower()
                for p in all_prospects():
                    if str(p.get("email") or "").strip().lower() == email:
                        title = title or str(p.get("title") or "").strip()
                        company = company or str(p.get("company") or "").strip()
                        name = name or str(p.get("name") or "").strip()
                        break
            except Exception:
                pass
        st.caption(f"To: {to_line}")
        if name:
            st.caption(f"Name: {name}")
        if title or company:
            bits = [b for b in (title, company) if b]
            st.caption(" · ".join(bits))
        if draft.get("gmail_draft_id") or str(draft.get("draft_id") or "").startswith("gmail:"):
            st.caption(f"Gmail draft · `{draft.get('gmail_draft_id') or draft.get('draft_id')}`")
    with cols[1]:
        if tid or has_pixel:
            st.markdown("🔒 **tracked** (open pixel)")
            if tid:
                st.code(tid[:18] + "…", language=None)
        else:
            st.warning("untracked — open pixel missing")

    tab_preview, tab_intel, tab_trace, tab_edit = st.tabs(
        ["Preview", "Intelligence", "Agent Trace", "Edit"]
    )

    with tab_preview:
        body = draft.get("body_html") or draft.get("html") or ""
        if not body:
            st.info("No HTML body stored for this draft.")
        else:
            st.markdown(body, unsafe_allow_html=True)
            if tid or has_pixel:
                st.success("Open-tracking pixel present in this draft.")
            else:
                st.error("No open-tracking pixel detected.")

        s1, s2 = st.columns(2)
        with s1:
            if st.button("📨 Send now", type="primary", key=f"{key_prefix}_send"):
                result = _send_draft_now(draft)
                if result.get("error"):
                    st.error(result["error"])
                else:
                    st.success(
                        f"Sent · message_id={result.get('message_id')} · "
                        f"tracking={result.get('tracking_id') or tid or '—'}"
                    )
                    # mark drive copy sent
                    try:
                        from core import drive_db

                        did = draft.get("draft_id")
                        if did:
                            draft["status"] = "sent"
                            draft["tracking_id"] = result.get("tracking_id") or tid
                            drive_db.save_draft(did, draft)
                    except Exception:
                        pass
                st.json(result)
        with s2:
            if st.button("🔒 Ensure tracking", key=f"{key_prefix}_ensure_track"):
                updated = _ensure_tracking(draft)
                draft.update(updated)
                st.success(f"Tracking id: {draft.get('tracking_id')}")
                st.rerun()

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
        if not brief and not ledger:
            st.caption("No intelligence brief attached (Chat/Gmail drafts are still sendable).")

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
            draft["has_open_pixel"] = True
            did = draft.get("draft_id")
            if did:
                drive_db.save_draft(did, draft)
                st.success(f"Saved · tracking_id preserved ({new_tid[:8]}…)")
            else:
                st.warning("No draft_id — local only")
            st.markdown(html, unsafe_allow_html=True)


def _ensure_tracking(draft: dict[str, Any]) -> dict[str, Any]:
    from core.tracking import inject_tracking
    from core import drive_db

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
    did = draft.get("draft_id")
    if did:
        try:
            drive_db.save_draft(did, draft)
        except Exception:
            pass
    # Also update Gmail draft MIME if applicable
    gmail_id = draft.get("gmail_draft_id") or (
        str(did).removeprefix("gmail:") if str(did).startswith("gmail:") else ""
    )
    if gmail_id:
        try:
            from gmail_client.drafts import _update_draft_html

            _update_draft_html(gmail_id, draft, html)
        except Exception:
            pass
    return {"body_html": html, "tracking_id": tid, "has_open_pixel": True}


def _send_draft_now(draft: dict[str, Any]) -> dict[str, Any]:
    """Send Drive or Gmail draft, always ensuring open-tracking pixel."""
    gmail_id = draft.get("gmail_draft_id") or ""
    did = str(draft.get("draft_id") or "")
    if not gmail_id and did.startswith("gmail:"):
        gmail_id = did.removeprefix("gmail:")

    # Prefer Gmail drafts.send so the existing MIME (with pixel) is used
    if gmail_id:
        from gmail_client.drafts import send_gmail_draft

        return send_gmail_draft(gmail_id)

    # Drive-only draft → send via Gmail API preserving tracking_id
    from gmail_client.send import send_email
    from core.tracking import inject_tracking

    to = draft.get("to") or draft.get("recipient") or ""
    if not to:
        return {"error": "missing recipient"}
    html, tid = inject_tracking(
        draft.get("body_html") or "",
        tracking_id=draft.get("tracking_id") or None,
        recipient_email=to,
        subject=draft.get("subject") or "",
        register=True,
    )
    return send_email(
        to=to,
        subject=draft.get("subject") or "(no subject)",
        html_body=html,
        recipient_name=draft.get("recipient_name") or "",
        tracking_id=tid,
        source=draft.get("source") or "drafts_page_send",
        from_email=draft.get("from") or None,
        cc=draft.get("cc") or None,
        include_signature=False,  # already in body if saved with sig
    )
