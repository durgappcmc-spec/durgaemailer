# NOTE: Shared draft inspector — WYSIWYG edit, attachments, preview, send.
from __future__ import annotations

import base64
from typing import Any, Optional

import streamlit as st

# Quill toolbar: normal email formatting (bold / font / bullets / …)
_QUILL_TOOLBAR = [
    [{"header": [1, 2, 3, False]}],
    [{"font": []}],
    [{"size": ["small", False, "large", "huge"]}],
    ["bold", "italic", "underline", "strike"],
    [{"color": []}, {"background": []}],
    [{"list": "ordered"}, {"list": "bullet"}],
    [{"align": []}],
    ["link"],
    ["clean"],
]


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
        if draft.get("gmail_draft_id") or str(draft.get("draft_id") or "").startswith(
            "gmail:"
        ):
            st.caption(
                f"Gmail draft · `{draft.get('gmail_draft_id') or draft.get('draft_id')}`"
            )
        atts = draft.get("attachments") or []
        if atts:
            names = [
                str(a.get("name") or a.get("filename") or "file")
                for a in atts
                if isinstance(a, dict)
            ]
            st.caption("📎 " + ", ".join(names[:8]) + ("…" if len(names) > 8 else ""))
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
        # Gmail drafts: verbatim body from fetch. Drive-only: existing HTML path.
        body = draft.get("body")
        if body is None or body == "":
            body = draft.get("body_cleaned") or ""
        if not body and draft.get("source") != "gmail_fetch":
            raw = draft.get("body_html") or draft.get("html") or ""
            if raw:
                try:
                    from gmail_client.html_format import prepare_draft_bodies

                    body, _h = prepare_draft_bodies(raw)
                except Exception:
                    body = ""
        draft["_preview_body"] = body or ""
        if not body:
            st.info("No body stored for this draft.")
        else:
            try:
                from gmail_client.html_format import render_draft_html

                st.markdown(
                    render_draft_html(
                        draft.get("subject") or "",
                        draft.get("to") or draft.get("recipient") or "",
                        draft.get("cc") or "",
                        body,
                        bcc=draft.get("shown_bcc") or draft.get("bcc") or "",
                        bcc_local=bool(draft.get("bcc_local")),
                    ),
                    unsafe_allow_html=True,
                )
            except Exception:
                st.markdown(
                    f'<div class="email-preview">{body}</div>',
                    unsafe_allow_html=True,
                )
            if tid or has_pixel:
                st.caption("🔒 Open tracking is embedded (hidden). Links shown as originals.")
            else:
                st.error("No open-tracking pixel detected.")

        s1, s2, s3 = st.columns(3)
        with s1:
            if st.button("📨 Send now", type="primary", key=f"{key_prefix}_send"):
                result = _send_draft_now(draft, rebuild=False)
                if result.get("error"):
                    st.error(result["error"])
                else:
                    st.success(
                        f"Sent · message_id={result.get('message_id')} · "
                        f"tracking={result.get('tracking_id') or tid or '—'}"
                    )
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
        with s3:
            if st.button("🗑 Remove draft", key=f"{key_prefix}_remove"):
                did = str(draft.get("draft_id") or "")
                gmail_id = draft.get("gmail_draft_id") or (
                    did.removeprefix("gmail:") if did.startswith("gmail:") else ""
                )
                errors = []
                if gmail_id:
                    try:
                        from gmail_client.drafts import delete_gmail_draft

                        res = delete_gmail_draft(gmail_id)
                        if res.get("error"):
                            errors.append(res["error"])
                    except Exception as e:
                        errors.append(str(e))
                if did:
                    try:
                        from core import drive_db

                        drive_db.delete_draft(did, purge=True)
                    except Exception as e:
                        errors.append(str(e))
                if errors:
                    st.error("; ".join(errors))
                else:
                    st.success("Draft removed")
                    if st.session_state.get("opened_draft_id") == did:
                        st.session_state.opened_draft_id = ""
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
            st.metric(
                "Confidence",
                f"{float(conf):.0%}" if float(conf) <= 1 else str(conf),
            )
        if not brief and not ledger:
            st.caption(
                "No intelligence brief attached (Chat/Gmail drafts are still sendable)."
            )

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
        gid = draft.get("gmail_draft_id") or ""
        did = str(draft.get("draft_id") or "")
        if not gid and did.startswith("gmail:"):
            gid = did.removeprefix("gmail:")
        if gid and draft.get("source") == "gmail_fetch":
            _render_gmail_edit_tab(draft, gid=gid, key_prefix=key_prefix)
        else:
            _render_edit_tab(draft, tid=tid, key_prefix=key_prefix)


def _render_gmail_edit_tab(
    draft: dict[str, Any], *, gid: str, key_prefix: str
) -> None:
    """Edit To/Cc/Bcc/subject/body from the same Gmail fetch as Preview."""
    from gmail_client.drafts import (
        delete_gmail_draft,
        fetch_gmail_draft,
        save_gmail_draft,
        send_draft,
    )
    from gmail_client.html_format import clean_email_body

    if "bcc_cache" not in st.session_state:
        st.session_state.bcc_cache = {}

    shown_bcc = draft.get("shown_bcc")
    if shown_bcc is None:
        shown_bcc = draft.get("bcc") or st.session_state.bcc_cache.get(gid, "")

    new_to = st.text_input(
        "To",
        value=draft.get("to") or "",
        key=f"to_{gid}",
        help="Comma-separated for multiple recipients",
    )
    new_cc = st.text_input(
        "Cc",
        value=draft.get("cc") or "",
        key=f"cc_{gid}",
        help="Comma-separated. Leave blank for none.",
    )
    new_bcc = st.text_input(
        "Bcc",
        value=shown_bcc,
        key=f"bcc_{gid}",
        help="Comma-separated. Blind recipients.",
    )
    new_subject = st.text_input(
        "Subject",
        value=draft.get("subject") or "",
        key=f"sub_{gid}",
    )
    new_body = st.text_area(
        "Body",
        value=draft.get("body") or "",
        height=400,
        key=f"body_{gid}",
    )
    draft["_edit_body"] = new_body or ""

    col_a, col_b, col_c = st.columns([1, 1, 1])
    save_clicked = col_a.button("💾 Save to Gmail", key=f"save_{gid}")
    send_clicked = col_b.button("📤 Send now", key=f"send_{gid}")
    disc_clicked = col_c.button("🗑️ Discard draft", key=f"disc_{gid}")

    if disc_clicked:
        res = delete_gmail_draft(gid)
        if res.get("error"):
            st.error(res["error"])
            return
        try:
            from core import drive_db

            drive_db.delete_draft(draft.get("draft_id") or f"gmail:{gid}", purge=True)
        except Exception:
            pass
        st.session_state.bcc_cache.pop(gid, None)
        _clear_gmail_edit_keys(gid)
        if st.session_state.get("opened_draft_id") in (
            draft.get("draft_id"),
            f"gmail:{gid}",
        ):
            st.session_state.opened_draft_id = ""
        st.success("Draft discarded")
        st.cache_data.clear()
        st.rerun()

    if not save_clicked and not send_clicked:
        return

    atts = _attachments_for_send(draft.get("attachments") or []) or None
    saved = save_gmail_draft(
        gid,
        new_to,
        new_cc,
        new_bcc,
        new_subject,
        new_body,
        attachments=atts,
        from_email=draft.get("from") or None,
    )
    if saved.get("error"):
        st.error(saved["error"])
        return

    st.session_state.bcc_cache[gid] = saved.get("bcc") or new_bcc or ""
    try:
        st.cache_data.clear()
    except Exception:
        pass

    fetched = fetch_gmail_draft(gid)
    cleaned = saved.get("body_cleaned") or clean_email_body(new_body)
    to_n = saved.get("to") or ""
    cc_n = saved.get("cc") or ""
    issues: list[str] = []
    if (fetched.get("body") or "").strip() != cleaned.strip():
        issues.append(
            "body mismatch\n--- written ---\n"
            f"{cleaned}\n--- gmail ---\n{fetched.get('body')}"
        )
    if (fetched.get("to") or "").lower() != to_n.lower():
        issues.append(f"To mismatch: written={to_n!r} gmail={fetched.get('to')!r}")
    if (fetched.get("cc") or "").lower() != cc_n.lower():
        issues.append(f"Cc mismatch: written={cc_n!r} gmail={fetched.get('cc')!r}")
    gmail_bcc = fetched.get("bcc") or ""
    want_bcc = saved.get("bcc") or ""
    if want_bcc and gmail_bcc and gmail_bcc.lower() != want_bcc.lower():
        print(f"[gmail] Bcc not echoed (written={want_bcc!r} gmail={gmail_bcc!r})")
    elif want_bcc and not gmail_bcc:
        print(f"[gmail] Bcc omitted by API (cached locally): {want_bcc!r}")

    if issues:
        st.error("Gmail save did not round-trip:\n\n" + "\n\n".join(issues))
        return

    _clear_gmail_edit_keys(gid)
    if send_clicked:
        result = send_draft(gid)
        if result.get("error"):
            st.error(result["error"])
            return
        st.success(f"Sent · message_id={result.get('message_id')}")
        st.session_state.bcc_cache.pop(gid, None)
        if st.session_state.get("opened_draft_id") in (
            draft.get("draft_id"),
            f"gmail:{gid}",
        ):
            st.session_state.opened_draft_id = ""
        st.rerun()

    st.success("Saved to Gmail")
    st.rerun()


def _clear_gmail_edit_keys(gid: str) -> None:
    for suffix in ("to", "cc", "bcc", "sub", "body"):
        st.session_state.pop(f"{suffix}_{gid}", None)


def _render_edit_tab(
    draft: dict[str, Any], *, tid: str, key_prefix: str
) -> None:
    from core.tracking import inject_tracking, strip_tracking
    from core import drive_db
    from gmail_client.attachments import files_to_attachments

    st.caption(
        "Edit like a normal email — bold, font, bullets, links. "
        "Tracking pixel is re-added automatically on save."
    )
    draft_key = str(draft.get("draft_id") or "local").replace(":", "_")
    k = f"{key_prefix}_{draft_key}"
    subject = st.text_input(
        "Subject",
        value=draft.get("subject") or "",
        key=f"{k}_subj",
    )

    raw_html = draft.get("body_html") or draft.get("html") or ""
    try:
        from gmail_client.html_format import normalize_email_html

        edit_html = normalize_email_html(strip_tracking(raw_html))
    except Exception:
        try:
            edit_html = strip_tracking(raw_html)
        except Exception:
            edit_html = raw_html

    body_html = _rich_text_editor(
        edit_html,
        key=f"{k}_quill",
    )

    existing = list(draft.get("attachments") or [])
    keep_flags: list[bool] = []
    if existing:
        st.markdown("**Current attachments**")
        for i, att in enumerate(existing):
            if not isinstance(att, dict):
                keep_flags.append(False)
                continue
            label = (
                f"{att.get('name') or att.get('filename') or 'file'} "
                f"({_fmt_size(att.get('size'))})"
            )
            keep_flags.append(
                st.checkbox(
                    label,
                    value=True,
                    key=f"{k}_keep_att_{i}_{att.get('name') or i}",
                )
            )
    else:
        st.caption("No files attached yet.")

    uploads = st.file_uploader(
        "Attach files",
        accept_multiple_files=True,
        key=f"{k}_upload",
        help="PDF, Word, Excel, images, etc. Included when you save / send.",
    )

    c1, c2 = st.columns(2)
    save = c1.button("💾 Save draft", type="primary", key=f"{k}_save")
    show_html = c2.checkbox(
        "Show HTML (advanced)",
        value=False,
        key=f"{k}_show_html",
    )
    if show_html:
        st.code(body_html or "", language="html")

    if not save:
        return

    kept = [a for a, keep in zip(existing, keep_flags) if keep and isinstance(a, dict)]
    new_atts = files_to_attachments(list(uploads) if uploads else [])
    # Dedupe by filename — new uploads replace same-named existing
    by_name: dict[str, dict] = {}
    for a in kept:
        by_name[str(a.get("name") or a.get("filename") or "").lower()] = a
    for a in new_atts:
        by_name[str(a.get("name") or "").lower()] = a
    merged = list(by_name.values())
    total = sum(int(a.get("size") or 0) for a in merged)
    if total > 20 * 1024 * 1024:
        st.error(
            f"Attachments total {_fmt_size(total)} — keep under 20 MB for Gmail."
        )
        return

    try:
        from gmail_client.html_format import normalize_email_html

        body_to_save = normalize_email_html(body_html or "")
    except Exception:
        body_to_save = body_html or ""
    html, new_tid = inject_tracking(
        body_to_save,
        tracking_id=tid or None,
        recipient_email=draft.get("to") or draft.get("recipient") or "",
        subject=subject,
        register=False,
        track_clicks=False,
        track_opens=True,
    )
    draft["subject"] = subject
    draft["body_html"] = html
    draft["tracking_id"] = new_tid
    draft["has_open_pixel"] = True
    draft["attachments"] = _attachments_for_storage(merged)

    did = draft.get("draft_id")
    if did:
        try:
            drive_db.save_draft(did, draft)
        except Exception as e:
            st.error(f"Drive save failed: {e}")
            return

    gmail_id = draft.get("gmail_draft_id") or (
        str(did).removeprefix("gmail:") if str(did or "").startswith("gmail:") else ""
    )
    if gmail_id:
        try:
            from gmail_client.drafts import _update_draft_html

            _update_draft_html(
                gmail_id,
                draft,
                html,
                attachments=_attachments_for_send(merged) or None,
                subject=subject,
            )
        except Exception as e:
            st.warning(f"Saved to Drive; Gmail update failed: {e}")

    st.success(
        f"Saved · tracking `{str(new_tid)[:8]}…` · "
        f"{len(merged)} attachment(s)"
    )
    st.markdown(html, unsafe_allow_html=True)
    st.rerun()


def _rich_text_editor(html: str, *, key: str) -> str:
    """WYSIWYG editor; falls back to text_area if Quill is unavailable."""
    try:
        from streamlit_quill import st_quill

        result = st_quill(
            value=html or "",
            html=True,
            toolbar=_QUILL_TOOLBAR,
            key=key,
            placeholder="Write your email…",
        )
        if result is None:
            return html or ""
        return str(result)
    except Exception as e:
        st.warning(
            f"Rich editor unavailable ({e}). Editing as HTML — "
            "install streamlit-quill for bold/bullets/fonts."
        )
        return st.text_area(
            "Email body (HTML)",
            value=html or "",
            height=320,
            key=f"{key}_fallback",
        )


def _fmt_size(n: Any) -> str:
    try:
        n = int(n or 0)
    except Exception:
        return "?"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _attachments_for_storage(atts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Persistable attachment rows (base64, no raw bytes)."""
    out: list[dict[str, Any]] = []
    for a in atts:
        if not isinstance(a, dict):
            continue
        name = a.get("name") or a.get("filename") or "file"
        mime = a.get("mime_type") or a.get("mimeType") or "application/octet-stream"
        b64 = a.get("data_base64") or ""
        data = a.get("data")
        if not b64 and data:
            if isinstance(data, str):
                b64 = data
            else:
                b64 = base64.b64encode(data).decode("ascii")
        size = a.get("size")
        if size is None and b64:
            try:
                size = len(base64.b64decode(b64))
            except Exception:
                size = 0
        out.append(
            {
                "name": name,
                "filename": name,
                "mime_type": mime,
                "mimeType": mime,
                "size": int(size or 0),
                "data_base64": b64,
            }
        )
    return out


def _attachments_for_send(atts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Binary attachments for Gmail MIME."""
    out: list[dict[str, Any]] = []
    for a in atts:
        if not isinstance(a, dict):
            continue
        data = a.get("data")
        if not data and a.get("data_base64"):
            try:
                data = base64.b64decode(a["data_base64"])
            except Exception:
                data = None
        if not data:
            continue
        if isinstance(data, str):
            data = data.encode("utf-8")
        out.append(
            {
                "name": a.get("name") or a.get("filename") or "file",
                "data": data,
                "mime_type": a.get("mime_type")
                or a.get("mimeType")
                or "application/octet-stream",
            }
        )
    return out


def _ensure_tracking(draft: dict[str, Any]) -> dict[str, Any]:
    from core.tracking import inject_tracking
    from core import drive_db

    html, tid = inject_tracking(
        draft.get("body_html") or "",
        tracking_id=draft.get("tracking_id") or None,
        recipient_email=draft.get("to") or draft.get("recipient") or "",
        subject=draft.get("subject") or "",
        register=True,
        track_clicks=False,
        track_opens=True,
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
    gmail_id = draft.get("gmail_draft_id") or (
        str(did).removeprefix("gmail:") if str(did or "").startswith("gmail:") else ""
    )
    if gmail_id:
        try:
            from gmail_client.drafts import _update_draft_html

            _update_draft_html(
                gmail_id,
                draft,
                html,
                attachments=_attachments_for_send(draft.get("attachments") or [])
                or None,
            )
        except Exception:
            pass
    return {"body_html": html, "tracking_id": tid, "has_open_pixel": True}


def _send_draft_now(draft: dict[str, Any], *, rebuild: bool = True) -> dict[str, Any]:
    """Send Drive or Gmail draft. Gmail send uses stored MIME (Cc/Bcc intact)."""
    gmail_id = draft.get("gmail_draft_id") or ""
    did = str(draft.get("draft_id") or "")
    if not gmail_id and did.startswith("gmail:"):
        gmail_id = did.removeprefix("gmail:")

    send_atts = _attachments_for_send(draft.get("attachments") or [])

    if gmail_id and (not rebuild or not send_atts):
        from gmail_client.drafts import send_draft

        return send_draft(gmail_id)

    # If we have attachments to add, refresh Gmail MIME first then send
    if gmail_id and send_atts:
        try:
            from gmail_client.drafts import _update_draft_html, send_gmail_draft
            from core.tracking import inject_tracking

            html, tid = inject_tracking(
                draft.get("body_html") or "",
                tracking_id=draft.get("tracking_id") or None,
                recipient_email=draft.get("to") or draft.get("recipient") or "",
                subject=draft.get("subject") or "",
                register=True,
            )
            _update_draft_html(
                gmail_id,
                draft,
                html,
                attachments=send_atts,
                subject=draft.get("subject") or "",
            )
            draft["body_html"] = html
            draft["tracking_id"] = tid
            return send_gmail_draft(gmail_id)
        except Exception:
            pass

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
        attachments=send_atts or None,
        include_signature=False,
    )
