# NOTE: Shared draft inspector — WYSIWYG edit, attachments, preview, send.
from __future__ import annotations

import base64
import re
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

_EDITOR_SCROLL_CSS = """
<style>
/* Scroll the Quill host, not a clipped iframe (Streamlit sets scrolling=no). */
div:has(> iframe[title*="quill" i]),
div:has(> iframe[title*="streamlit_quill" i]),
[data-testid="stCustomComponentV1"]:has(iframe[title*="quill" i]),
[data-testid="stCustomComponentV1"]:has(iframe[title*="streamlit_quill" i]) {
  max-height: 560px !important;
  overflow-y: scroll !important;
  overflow-x: hidden !important;
  overscroll-behavior: contain !important;
  border: 1px solid #dadce0;
  border-radius: 8px;
}
iframe[title*="quill" i],
iframe[title*="streamlit_quill" i] {
  min-height: 240px !important;
  max-height: none !important;
  overflow: visible !important;
}
div[data-testid="stHtml"] iframe {
  overflow: auto !important;
}
</style>
"""

_EDITOR_SCROLL_JS = """
<!doctype html>
<html><head><meta charset="utf-8"></head><body>
<script>
(function () {
  var parentDoc = window.parent && window.parent.document;
  if (!parentDoc) return;
  function isQuillFrame(f, doc) {
    var title = (f.getAttribute("title") || "").toLowerCase();
    if (title.indexOf("quill") !== -1) return true;
    return !!(doc && doc.querySelector && doc.querySelector(".ql-editor"));
  }
  function patch() {
    parentDoc.querySelectorAll("iframe").forEach(function (f) {
      var doc = null;
      try { doc = f.contentDocument || (f.contentWindow && f.contentWindow.document); }
      catch (e) { doc = null; }
      if (!isQuillFrame(f, doc)) return;
      f.setAttribute("scrolling", "yes");
      var wrap = f.parentElement;
      if (wrap) {
        wrap.style.maxHeight = "560px";
        wrap.style.overflowY = "scroll";
        wrap.style.overflowX = "hidden";
        wrap.style.overscrollBehavior = "contain";
      }
      if (!doc) return;
      var editor = doc.querySelector(".ql-editor");
      if (!editor) return;
      if (!doc.getElementById("relay-quill-scroll")) {
        var s = doc.createElement("style");
        s.id = "relay-quill-scroll";
        s.textContent = [
          ".ql-toolbar.ql-snow { flex: 0 0 auto !important; position: sticky !important; top: 0 !important; z-index: 2 !important; background: #fff !important; }",
          ".ql-container.ql-snow { height: auto !important; overflow: visible !important; }",
          ".ql-editor { max-height: 420px !important; min-height: 180px !important; overflow-y: scroll !important; overflow-x: hidden !important; overscroll-behavior: contain !important; }"
        ].join("\\n");
        (doc.head || doc.documentElement).appendChild(s);
      }
      editor.style.maxHeight = "420px";
      editor.style.overflowY = "scroll";
    });
  }
  patch();
  var n = 0;
  var iv = setInterval(function () {
    patch();
    if (++n > 40) clearInterval(iv);
  }, 250);
  try {
    var obs = new MutationObserver(patch);
    obs.observe(parentDoc.body, { childList: true, subtree: true });
  } catch (e) {}
})();
</script>
</body></html>
"""


def enable_editor_mouse_scroll(*, inject_js: bool = True) -> None:
    """Let the mouse wheel scroll Quill / preview iframes under a draft."""
    st.markdown(_EDITOR_SCROLL_CSS, unsafe_allow_html=True)
    if not inject_js:
        return
    try:
        import streamlit.components.v1 as components

        components.html(_EDITOR_SCROLL_JS, height=1, scrolling=False)
    except Exception:
        pass


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
        if tid:
            st.markdown("🔒 **tracked** (pixel added at send)")
            if tid:
                st.code(tid[:18] + "…", language=None)
        else:
            st.caption("Open tracking is added when you send.")

    section = st.radio(
        "Draft view",
        ["Preview", "Intelligence", "Agent Trace", "Edit"],
        horizontal=True,
        key=f"{key_prefix}_section",
        label_visibility="collapsed",
    )

    if section == "Preview":
        # Inject Gmail HTML as-is — Streamlit markdown must not escape the body.
        body_html = draft.get("body_html") or draft.get("html") or ""
        if not body_html:
            plain = draft.get("body") or draft.get("body_cleaned") or ""
            if plain:
                try:
                    from gmail_client.html_format import (
                        html_from_cleaned_body,
                        looks_like_html,
                    )

                    body_html = (
                        plain if looks_like_html(plain) else html_from_cleaned_body(plain)
                    )
                except Exception:
                    body_html = plain
        draft["_preview_body"] = body_html or ""
        if not body_html:
            st.info("No body stored for this draft.")
        else:
            enable_editor_mouse_scroll(inject_js=False)
            try:
                import streamlit.components.v1 as components
                from gmail_client.html_format import render_gmail_preview
                from core.tracking import html_for_preview

                preview_html = html_for_preview(body_html)
                preview = render_gmail_preview(
                    draft.get("subject") or "",
                    draft.get("to") or draft.get("recipient") or "",
                    draft.get("cc") or "",
                    preview_html,
                    bcc=draft.get("shown_bcc") or draft.get("bcc") or "",
                    bcc_local=bool(draft.get("bcc_local")),
                )
                doc = (
                    "<!doctype html><html><head><meta charset='utf-8'></head>"
                    "<body style='margin:0;padding:8px;background:#fff;"
                    "overflow-y:auto;overscroll-behavior:contain'>"
                    f"{preview}</body></html>"
                )
                blocks = (
                    body_html.count("<p")
                    + body_html.count("<div")
                    + body_html.count("<br")
                    + body_html.count("\n")
                )
                height = min(720, max(380, 180 + blocks * 18))
                components.html(doc, height=height, scrolling=True)
                enable_editor_mouse_scroll()
            except Exception:
                try:
                    from core.tracking import html_for_preview as _preview_html

                    safe = _preview_html(body_html)
                except Exception:
                    safe = body_html
                st.markdown(
                    f'<div class="gm-preview">{safe}</div>',
                    unsafe_allow_html=True,
                )
            if tid:
                st.caption("Open tracking is added when you send — viewing a draft does not count.")
            else:
                st.caption("Open tracking will be attached at send.")

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
                mid = draft.get("gmail_message_id") or (
                    did.removeprefix("gmail-msg:") if did.startswith("gmail-msg:") else ""
                )
                errors = []
                if gmail_id or mid:
                    try:
                        from gmail_client.drafts import delete_gmail_item

                        res = delete_gmail_item(
                            gmail_draft_id=gmail_id, gmail_message_id=mid
                        )
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

    if section == "Intelligence":
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

    if section == "Agent Trace":
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

    if section == "Edit":
        enable_editor_mouse_scroll()
        gid = draft.get("gmail_draft_id") or ""
        did = str(draft.get("draft_id") or "")
        if not gid and did.startswith("gmail:"):
            gid = did.removeprefix("gmail:")
        if gid and draft.get("source") == "gmail_fetch":
            _render_gmail_edit_tab(draft, gid=gid, key_prefix=key_prefix)
        else:
            _render_edit_tab(draft, tid=tid, key_prefix=key_prefix)


_GMAIL_QUILL_TOOLBAR = [
    ["bold", "italic", "underline", "strike"],
    [{"list": "ordered"}, {"list": "bullet"}],
    [{"indent": "-1"}, {"indent": "+1"}],
    ["link", "blockquote"],
    [{"header": [1, 2, 3, False]}],
    [{"color": []}, {"background": []}],
    ["clean"],
]


def _render_gmail_edit_tab(
    draft: dict[str, Any], *, gid: str, key_prefix: str
) -> None:
    """Edit To/Cc/Bcc/subject/HTML body from the same Gmail fetch as Preview."""
    from core.signatures import (
        load_signatures,
        replace_signature,
        save_signature,
    )
    from gmail_client.drafts import (
        delete_gmail_draft,
        fetch_gmail_draft,
        save_gmail_draft,
        send_draft,
    )
    from gmail_client.html_format import (
        clean_email_body,
        html_for_editor,
        html_from_cleaned_body,
        looks_like_html,
        plain_from_html,
    )

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

    user_email = (
        draft.get("from")
        or st.session_state.get("gmail_profile_email")
        or ""
    )
    sigs = load_signatures(user_email)
    sig_ids = list(sigs.keys())
    seed_key = f"quill_seed_{gid}"
    nonce_key = f"quill_nonce_{gid}"
    prev_sig_key = f"sig_prev_{gid}"
    if seed_key not in st.session_state:
        from gmail_client.drafts import _bodies_are_blank

        html0 = html_for_editor(draft.get("body_html") or "")
        if _bodies_are_blank(html0, ""):
            plain = draft.get("body") or draft.get("body_text") or ""
            if looks_like_html(plain):
                html0 = html_for_editor(plain)
            elif plain.strip():
                html0 = html_from_cleaned_body(plain)
        st.session_state[seed_key] = html0
        st.session_state[nonce_key] = 0

    nonce = int(st.session_state.get(nonce_key, 0) or 0)
    quill_key = f"quill_{gid}_{nonce}"
    seed_html = st.session_state.get(seed_key) or draft.get("body_html") or ""
    new_body = _rich_text_editor(
        seed_html,
        key=quill_key,
        toolbar=_GMAIL_QUILL_TOOLBAR,
        keep_seed_if_empty=False,
    )
    if (seed_html or "").strip() and _quill_result_empty(new_body):
        st.warning(
            "Rich editor did not show the Gmail body. Edit the text below — "
            "Save still writes to Gmail."
        )
        new_body = st.text_area(
            "Email body",
            value=seed_html,
            height=400,
            key=f"body_fallback_{gid}_{nonce}",
        )
    draft["_edit_body"] = new_body or ""

    sig_col, edit_col = st.columns([4, 1])
    with sig_col:
        default_idx = 0
        try:
            from core.mail_prefs import signature_mode

            mode = signature_mode()
        except Exception:
            mode = "gmail"
        if "gmail" in sig_ids and mode == "gmail":
            default_idx = sig_ids.index("gmail")
        elif "none" in sig_ids and mode == "none":
            default_idx = sig_ids.index("none")
        elif "default" in sig_ids:
            default_idx = sig_ids.index("default")
        sig_choice = st.selectbox(
            "Signature",
            sig_ids,
            index=default_idx,
            format_func=lambda k: (sigs.get(k) or {}).get("name") or k,
            key=f"sig_{gid}",
        )
    with edit_col:
        st.write("")
        edit_sig = st.button("✏️ Edit", key=f"sig_edit_btn_{gid}", disabled=sig_choice in ("none", "gmail"))

    if st.session_state.get(prev_sig_key) is None:
        st.session_state[prev_sig_key] = sig_choice
    elif sig_choice != st.session_state[prev_sig_key]:
        current = new_body or st.session_state.get(seed_key) or ""
        st.session_state[seed_key] = replace_signature(
            str(current), (sigs.get(sig_choice) or {}).get("html") or ""
        )
        st.session_state[nonce_key] = nonce + 1
        st.session_state[prev_sig_key] = sig_choice
        st.rerun()

    if edit_sig:
        st.session_state[f"sig_editing_{gid}"] = True
    if st.session_state.get(f"sig_editing_{gid}") and sig_choice != "none":
        st.caption(f"Editing signature: {(sigs.get(sig_choice) or {}).get('name')}")
        edited_sig = _rich_text_editor(
            (sigs.get(sig_choice) or {}).get("html") or "",
            key=f"sig_quill_{gid}_{sig_choice}",
            toolbar=[["bold", "italic", "underline", "link"], ["clean"]],
        )
        s1, s2 = st.columns(2)
        if s1.button("Save signature", key=f"sig_save_{gid}"):
            save_signature(
                user_email,
                sig_choice,
                name=(sigs.get(sig_choice) or {}).get("name") or sig_choice,
                html=edited_sig or "",
            )
            current = new_body or st.session_state.get(seed_key) or ""
            st.session_state[seed_key] = replace_signature(str(current), edited_sig or "")
            st.session_state[nonce_key] = nonce + 1
            st.session_state[f"sig_editing_{gid}"] = False
            st.rerun()
        if s2.button("Cancel", key=f"sig_cancel_{gid}"):
            st.session_state[f"sig_editing_{gid}"] = False
            st.rerun()

    existing_atts, keep_flags, uploads = _render_attachment_editor(
        draft.get("attachments") or [],
        key=f"att_{gid}",
    )

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

    merged = merge_draft_attachments(existing_atts, keep_flags, uploads)
    total = _attachment_total_bytes(merged)
    if total > 20 * 1024 * 1024:
        st.error(
            f"Attachments total {_fmt_size(total)} — keep under 20 MB for Gmail."
        )
        return
    atts = _attachments_for_send(merged) or None
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
    cleaned = (
        saved.get("body_cleaned")
        or clean_email_body(plain_from_html(new_body or ""))
    )
    to_n = saved.get("to") or ""
    cc_n = saved.get("cc") or ""
    issues: list[str] = []
    gmail_plain = (fetched.get("body_text") or fetched.get("body") or "").strip()
    if gmail_plain != (cleaned or "").strip():
        issues.append(
            "body mismatch\n--- written ---\n"
            f"{cleaned}\n--- gmail ---\n{gmail_plain}"
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

    gmail_atts = fetched.get("attachments") or []
    if atts and not gmail_atts:
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
        try:
            st.cache_data.clear()
        except Exception:
            pass
        fetched = fetch_gmail_draft(gid)
        gmail_atts = fetched.get("attachments") or []

    if issues:
        st.error("Gmail save did not round-trip:\n\n" + "\n\n".join(issues))
        return

    draft["attachments"] = _attachments_for_storage(merged)
    try:
        from core import drive_db

        drive_db.save_draft(draft.get("draft_id") or f"gmail:{gid}", draft)
    except Exception:
        pass

    _clear_gmail_edit_keys(gid)
    if atts and not gmail_atts:
        st.warning(
            "Saved the draft text, but Gmail did not keep the attachment. "
            "Open Edit and click Save to Gmail again."
        )
        st.rerun()
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

    st.success(
        "Saved to Gmail"
        + (f" · {len(merged)} attachment(s)" if merged else "")
    )
    st.rerun()


def _clear_gmail_edit_keys(gid: str) -> None:
    prefixes = (
        f"to_{gid}",
        f"cc_{gid}",
        f"bcc_{gid}",
        f"sub_{gid}",
        f"body_{gid}",
        f"quill_{gid}",
        f"quill_seed_{gid}",
        f"quill_nonce_{gid}",
        f"sig_{gid}",
        f"sig_prev_{gid}",
        f"sig_edit_btn_{gid}",
        f"sig_quill_{gid}",
        f"sig_save_{gid}",
        f"sig_cancel_{gid}",
        f"sig_editing_{gid}",
        f"att_{gid}",
    )
    for k in list(st.session_state.keys()):
        sk = str(k)
        if any(sk == p or sk.startswith(p) for p in prefixes):
            st.session_state.pop(k, None)


def _render_edit_tab(
    draft: dict[str, Any], *, tid: str, key_prefix: str
) -> None:
    from core.tracking import inject_tracking, strip_tracking
    from core import drive_db

    st.caption(
        "Edit like a normal email — bold, font, bullets, links. "
        "Tracking is added when you send — saving a draft does not count as an open."
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

    existing, keep_flags, uploads = _render_attachment_editor(
        draft.get("attachments") or [],
        key=f"{k}_att",
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

    kept_merged = merge_draft_attachments(existing, keep_flags, uploads)
    total = _attachment_total_bytes(kept_merged)
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
        track_opens=False,
    )
    try:
        from core.tracking import prepare_draft_tracking

        html, new_tid = prepare_draft_tracking(html, new_tid)
    except Exception:
        pass
    draft["subject"] = subject
    draft["body_html"] = html
    draft["tracking_id"] = new_tid
    draft["has_open_pixel"] = False
    draft["attachments"] = _attachments_for_storage(kept_merged)

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
                attachments=_attachments_for_send(kept_merged) or None,
                subject=subject,
            )
        except Exception as e:
            st.warning(f"Saved to Drive; Gmail update failed: {e}")

    st.success(
        f"Saved · tracking `{str(new_tid)[:8]}…` · "
        f"{len(kept_merged)} attachment(s)"
    )
    st.markdown(html, unsafe_allow_html=True)
    st.rerun()


def _quill_result_empty(html: str) -> bool:
    compact = re.sub(r"\s+", "", html or "", flags=re.I)
    return compact in ("", "<p></p>", "<p><br></p>", "<p><br/></p>")


def _rich_text_editor(
    html: str,
    *,
    key: str,
    toolbar: Optional[list] = None,
    keep_seed_if_empty: bool = True,
) -> str:
    """WYSIWYG editor; falls back to text_area if Quill is unavailable."""
    enable_editor_mouse_scroll(inject_js=False)
    try:
        from streamlit_quill import st_quill

        result = st_quill(
            value=html or "",
            html=True,
            toolbar=toolbar or _QUILL_TOOLBAR,
            key=key,
            placeholder="Write your email…",
        )
        enable_editor_mouse_scroll()
        st.caption("Scroll inside the editor to see the rest of the email.")
        # First paint / hidden iframe often returns "" — do not wipe Gmail HTML.
        if result is None or _quill_result_empty(str(result)):
            if keep_seed_if_empty and (html or "").strip() and not _quill_result_empty(html):
                return html
            return str(result or "")
        return str(result)
    except Exception as e:
        st.warning(
            f"Rich editor unavailable ({e}). Editing as HTML — "
            "install streamlit-quill for bold/bullets/fonts."
        )
        return st.text_area(
            "Email body (HTML)",
            value=html or "",
            height=400,
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


def merge_draft_attachments(
    existing: list[Any],
    keep_flags: list[bool],
    new_atts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep checked files; new uploads replace the same filename."""
    kept = [
        a
        for a, keep in zip(existing, keep_flags)
        if keep and isinstance(a, dict)
    ]
    by_name: dict[str, dict[str, Any]] = {}
    for a in kept:
        by_name[str(a.get("name") or a.get("filename") or "").lower()] = a
    for a in new_atts or []:
        if not isinstance(a, dict):
            continue
        by_name[str(a.get("name") or a.get("filename") or "").lower()] = a
    return [a for k, a in by_name.items() if k]


def _attachment_total_bytes(atts: list[dict[str, Any]]) -> int:
    return sum(int(a.get("size") or 0) for a in atts if isinstance(a, dict))


def _render_attachment_editor(
    existing: list[Any], *, key: str
) -> tuple[list[dict[str, Any]], list[bool], list[dict[str, Any]]]:
    """Checkboxes for current files plus a multi-file uploader.

    New uploads are staged in session so a Quill rerun does not drop the bytes
    before Save to Gmail.
    """
    from gmail_client.attachments import files_to_attachments

    rows = [a for a in (existing or []) if isinstance(a, dict)]
    keep_flags: list[bool] = []
    if rows:
        st.markdown("**Attachments**")
        for i, att in enumerate(rows):
            label = (
                f"{att.get('name') or att.get('filename') or 'file'} "
                f"({_fmt_size(att.get('size'))})"
            )
            keep_flags.append(
                st.checkbox(
                    label,
                    value=True,
                    key=f"{key}_keep_{i}_{att.get('name') or i}",
                )
            )
    else:
        st.caption("No files attached yet.")
    uploads = st.file_uploader(
        "Attach files",
        accept_multiple_files=True,
        key=f"{key}_upload",
        help="PDF, Word, Excel, images, etc. Saved onto the Gmail draft when you click Save.",
    )
    staged_key = f"{key}_staged"
    if uploads:
        st.session_state[staged_key] = files_to_attachments(list(uploads))
    staged = [
        a
        for a in (st.session_state.get(staged_key) or [])
        if isinstance(a, dict)
    ]
    if staged:
        names = ", ".join(
            str(a.get("name") or a.get("filename") or "file") for a in staged
        )
        st.caption(f"Ready to save: {names}")
    return rows, keep_flags, staged


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
    from core.tracking import prepare_draft_tracking
    from core import drive_db

    html, tid = prepare_draft_tracking(
        draft.get("body_html") or "",
        draft.get("tracking_id") or None,
    )
    draft["body_html"] = html
    draft["tracking_id"] = tid
    draft["has_open_pixel"] = False
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
    return {"body_html": html, "tracking_id": tid, "has_open_pixel": False}


def _send_draft_now(draft: dict[str, Any], *, rebuild: bool = True) -> dict[str, Any]:
    """Send Drive or Gmail draft. Gmail send uses stored MIME (Cc/Bcc intact)."""
    gmail_id = draft.get("gmail_draft_id") or ""
    did = str(draft.get("draft_id") or "")
    if not gmail_id and did.startswith("gmail:"):
        gmail_id = did.removeprefix("gmail:")

    send_atts = _attachments_for_send(draft.get("attachments") or [])

    if gmail_id:
        # Persist UI files first if needed; send_gmail_draft injects tracking
        # without dropping Gmail attachment parts.
        if rebuild and send_atts:
            try:
                from gmail_client.drafts import _update_draft_html

                _update_draft_html(
                    gmail_id,
                    draft,
                    draft.get("body_html") or "",
                    attachments=send_atts,
                    subject=draft.get("subject") or "",
                )
            except Exception:
                pass
        from gmail_client.drafts import send_draft

        return send_draft(gmail_id)

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
