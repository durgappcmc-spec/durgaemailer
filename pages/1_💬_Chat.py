# NOTE: Streaming UI captures __meta__ dicts separately from text chunks.
# Stop / Edit / Clear sit in the composer bar above st.chat_input.
# Files come only from st.chat_input paperclip; staged across turns until cleared/used.
# Load path stays light: no Gmail sync / no heavy router import until the user sends.
from __future__ import annotations

import streamlit as st

from config import APP_NAME
from core.auth_ui import logout_button, require_login
from core import durable_store

st.set_page_config(page_title=f"Chat · {APP_NAME}", page_icon="💬", layout="wide")
if not require_login():
    st.stop()
logout_button()

# Genspark CSS + usage rail
try:
    from pathlib import Path

    css = Path(__file__).resolve().parents[1] / "static" / "genspark.css"
    if css.is_file():
        st.markdown(f"<style>{css.read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)
except Exception:
    pass

with st.sidebar:
    st.markdown('<div class="genspark-rail">', unsafe_allow_html=True)
    try:
        from core.chat_llm import (
            genspark_ready,
            hydrate_into,
            load_provider,
            save_provider,
        )

        hydrate_into(st.session_state)
        picked = st.radio(
            "Chat model",
            ["gemini", "genspark"],
            format_func=lambda k: "Gemini" if k == "gemini" else "Genspark",
            key="chat_llm_provider",
            horizontal=True,
            help="Saved as your default. Chat keeps using this until you change it.",
        )
        if picked != load_provider():
            save_provider(picked)
        if picked == "genspark" and not genspark_ready():
            st.caption("Genspark key missing — Chat will use Gemini until GSK_API_KEY is set.")
        else:
            st.caption("Saved. Chat will keep using this model.")
    except Exception:
        st.caption("Chat model: Gemini")
    try:
        from core.mail_prefs import render_sidebar_signature_pref

        st.subheader("Signature")
        render_sidebar_signature_pref()
    except Exception:
        st.caption("Signature: Gmail account")
    st.subheader("Bulk jobs")
    try:
        from core import drive_db

        for j in drive_db.list_bulk_jobs(limit=5):
            st.caption(f"{j.get('job_id')} · {j.get('current_phase')}")
        gm = drive_db.gemini_usage_mtd()
        st.subheader("Gemini MTD")
        st.caption(
            f"calls={gm.get('totals', {}).get('calls', 0)} · "
            f"in={gm.get('totals', {}).get('tokens_in', 0)} · "
            f"out={gm.get('totals', {}).get('tokens_out', 0)}"
        )
        for kind, b in (gm.get("by_task_kind") or {}).items():
            st.caption(f"{kind}: {b.get('calls', 0)}")
        zi = drive_db.zoominfo_usage_mtd()
        st.caption(f"ZI credits MTD: {zi.get('credits', 0)}")
    except Exception:
        st.caption("Drive usage unavailable")
    try:
        if "gmail_profile_email" not in st.session_state:
            from gmail_client.drafts import gmail_profile_email

            st.session_state["gmail_profile_email"] = gmail_profile_email()
        acct = st.session_state.get("gmail_profile_email") or ""
        if acct:
            st.caption(f"Gmail: {acct}")
    except Exception:
        pass
    st.markdown("</div>", unsafe_allow_html=True)

st.title("💬 Chat")
st.caption(
    "Paperclip = file **context** (not attached unless you say “attach the file”). "
    "Stop / Edit / Clear sit under the chat, next to where you type. "
    "Drafts from csr@karunamedia.org — `cc a@x.com and b@y.com`; `ignore addr@x.com` skips it."
)

_dbg = st.session_state.get("draft_debug")
if _dbg:
    with st.expander("Debug · draft recipients", expanded=False):
        st.write(f"user_message: `{_dbg.get('user_message') or ''}`")
        st.write("parsed_directives:")
        st.json(_dbg.get("parsed_directives") or {})
        st.write(f"recipients_final: `{_dbg.get('recipients_final')}`")
        st.write(f"draft_path: `{_dbg.get('draft_path') or '—'}`")
        st.write(
            f"ignored_count (session prospects not drafted): "
            f"`{_dbg.get('ignored_count')}`"
        )

# ---- session defaults ----
for key, default in (
    ("messages", []),
    ("staged_attachments", []),
    ("pending_user_msg", ""),
    ("need_file", False),
    ("last_mailbox", []),
    ("last_prospects", []),
    ("run_cancel", False),
    ("run_active", False),
    ("show_edit", False),
    ("edit_text", ""),
    ("_durable_hydrated", False),
    ("_durable_pull_started", False),
):
    if key not in st.session_state:
        st.session_state[key] = default

# Instant local restore (no Gmail). Drive pull if local chat is empty.
durable_store.hydrate_session_fast(st.session_state)
if not st.session_state.messages:
    with st.spinner("Restoring saved chat…"):
        durable_store.hydrate_chat_from_sheets_if_empty(st.session_state)
if not st.session_state._durable_pull_started:
    st.session_state._durable_pull_started = True
    durable_store.pull_sheets_into_local_async()
# Restore RAG memory + prospect list from Google Drive (Render disk is ephemeral)
if not st.session_state.get("_memory_hydrated"):
    st.session_state._memory_hydrated = True
    try:
        from core import memory as _mem

        _mem.hydrate_from_cloud()
    except Exception:
        pass
if not st.session_state.get("_prospects_drive_hydrated"):
    st.session_state._prospects_drive_hydrated = True
    try:
        from core.prospect_list import reload_from_drive

        st.session_state["_prospects_restored_n"] = reload_from_drive()
    except Exception:
        pass

mailbox_n = len(st.session_state.get("last_mailbox") or [])
if mailbox_n:
    st.caption(f"Mailbox context loaded: **{mailbox_n}** messages (for filters / follow-ups).")

# Keep composer controls visually tight against the bottom chat input
st.markdown(
    """
<style>
.relay-composer {
  position: sticky;
  bottom: 5.5rem;
  z-index: 100;
  background: var(--background-color, #0e1117);
  padding: 0.35rem 0 0.15rem 0;
  border-top: 1px solid rgba(128,128,128,0.25);
  margin-top: 0.5rem;
}
.relay-composer .stButton > button {
  min-height: 2.2rem;
}
</style>
""",
    unsafe_allow_html=True,
)

staged = st.session_state.staged_attachments or []
if st.session_state.get("need_file"):
    st.info(
        "Upload a file with the **paperclip** on the chat box, then send your "
        "message again (or tap **Continue pending request** after attaching)."
    )

if staged:
    names = []
    for a in staged:
        label = a.get("name") or "file"
        label += " (context ready)" if a.get("has_context") else " (attach only)"
        names.append(label)
    c1, c2 = st.columns([5, 1])
    c1.success("Ready: " + ", ".join(names))
    if c2.button("Clear files", use_container_width=True, key="clear_files_top"):
        st.session_state.staged_attachments = []
        st.session_state.need_file = False
        st.session_state.pending_user_msg = ""
        st.rerun()
    pending = (st.session_state.get("pending_user_msg") or "").strip()
    if pending and st.button("Continue pending request", type="primary", key="continue_pending"):
        st.session_state.force_prompt = pending
        st.session_state.pending_user_msg = ""
        st.session_state.need_file = False
        st.rerun()

# ---- message history (cap render for speed) ----
_DISPLAY_MSGS = 40
_all_msgs = st.session_state.messages or []
if len(_all_msgs) > _DISPLAY_MSGS:
    st.caption(f"Showing last {_DISPLAY_MSGS} of {len(_all_msgs)} messages")
for msg in _all_msgs[-_DISPLAY_MSGS:]:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        previews = (msg.get("meta") or {}).get("draft_previews") or []
        if previews:
            try:
                from gmail_client.html_format import render_draft_html

                for pv in previews:
                    st.markdown(
                        render_draft_html(
                            pv.get("subject") or "",
                            pv.get("to") or "",
                            pv.get("cc") or "",
                            pv.get("body_cleaned") or "",
                        ),
                        unsafe_allow_html=True,
                    )
            except Exception:
                pass
        files = msg.get("files") or []
        if files:
            st.caption("Used files: " + ", ".join(files))
        meta = msg.get("meta")
        if meta:
            sources = meta.get("sources") or []
            if sources:
                with st.expander(f"🔗 Sources ({len(sources)})"):
                    for s in sources:
                        title = s.get("title") or s.get("url") or "source"
                        url = s.get("url") or ""
                        stype = s.get("type") or "web"
                        if url:
                            st.markdown(f"- **{title}** ({stype}) — {url}")
                        else:
                            st.markdown(f"- **{title}** ({stype})")
            if meta.get("routing"):
                st.caption(f"Routing: `{meta['routing']}`")
            if meta.get("cancelled"):
                st.caption("Stopped by user")

# ---- composer: edit panel + Stop/Edit/Clear + chat input ----
st.markdown('<div class="relay-composer">', unsafe_allow_html=True)

if st.session_state.get("show_edit"):
    st.markdown("**✏️ Edit message**")
    edited = st.text_area(
        "Edit your message",
        height=120,
        key="edit_area",
        label_visibility="collapsed",
        placeholder="Revise your last message…",
    )
    e1, e2, _ = st.columns([1, 1, 3])
    if e1.button("▶ Save & run", type="primary", use_container_width=True, key="edit_save"):
        text = (edited or "").strip()
        if text:
            msgs = st.session_state.messages
            if msgs and msgs[-1].get("role") == "assistant":
                msgs.pop()
            if msgs and msgs[-1].get("role") == "user":
                msgs.pop()
            st.session_state.messages = msgs
            st.session_state.show_edit = False
            st.session_state.edit_text = ""
            st.session_state.run_cancel = False
            st.session_state.force_prompt = text
            st.rerun()
    if e2.button("Cancel", use_container_width=True, key="edit_cancel"):
        st.session_state.show_edit = False
        st.session_state.edit_text = ""
        st.rerun()

has_user = any(m.get("role") == "user" for m in st.session_state.messages)
tb1, tb2, tb3 = st.columns([1, 1, 1])
with tb1:
    stop_clicked = st.button(
        "⏹ Stop",
        type="primary",
        use_container_width=True,
        key="composer_stop",
        help="Cancel the current operation.",
    )
with tb2:
    edit_clicked = st.button(
        "✏️ Edit",
        use_container_width=True,
        key="composer_edit",
        help="Edit your last message and run again.",
        disabled=not has_user,
    )
with tb3:
    clear_clicked = st.button(
        "🗑️ Clear",
        use_container_width=True,
        key="composer_clear",
        help="Clear the conversation (keeps staged files).",
    )

st.markdown("</div>", unsafe_allow_html=True)

if stop_clicked:
    st.session_state.run_cancel = True
    st.session_state.run_active = False
    msgs = st.session_state.messages
    if msgs and msgs[-1].get("role") == "user":
        st.session_state.messages.append(
            {
                "role": "assistant",
                "content": "_(⏹ Stopped. Use **Edit** under the chat box to revise and resubmit.)_",
                "meta": {"routing": "STOPPED", "cancelled": True},
            }
        )
    elif msgs and msgs[-1].get("role") == "assistant":
        content = msgs[-1].get("content") or ""
        if "⏹ Stopped" not in content:
            msgs[-1]["content"] = content + "\n\n_(⏹ Stopped by you.)_"
            meta = dict(msgs[-1].get("meta") or {})
            meta["cancelled"] = True
            msgs[-1]["meta"] = meta
    st.toast("Stopped")
    st.rerun()

if clear_clicked:
    st.session_state.messages = []
    st.session_state.run_cancel = False
    st.session_state.run_active = False
    st.session_state.show_edit = False
    st.session_state.edit_text = ""
    try:
        durable_store.clear_chat_messages()
    except Exception:
        pass
    st.rerun()

if edit_clicked:
    last_user = ""
    for m in reversed(st.session_state.messages):
        if m.get("role") == "user":
            last_user = m.get("content") or ""
            break
    st.session_state.edit_text = last_user
    st.session_state.edit_area = last_user
    st.session_state.show_edit = True
    st.rerun()

prompt = st.chat_input(
    "Ask anything… paperclip to add files · Stop / Edit / Clear are just above",
    accept_file="multiple",
    file_type=None,
)

force = (st.session_state.pop("force_prompt", None) or "").strip()
if force and not prompt:

    class _Forced:
        text = force
        files = []

    prompt = _Forced()

if prompt:
    from agent.router import answer  # lazy — keeps Chat page open fast
    from gmail_client.attachments import files_to_attachments

    if hasattr(prompt, "text"):
        user_text = (prompt.text or "").strip()
        chat_files = list(getattr(prompt, "files", None) or [])
    else:
        user_text = str(prompt).strip()
        chat_files = []

    staged = list(st.session_state.get("staged_attachments") or [])
    if chat_files:
        with st.spinner("Reading uploaded files (PDF / docs)…"):
            paperclip = files_to_attachments(chat_files)
    else:
        paperclip = []

    by_name: dict = {a.get("name"): a for a in staged}
    for a in paperclip:
        by_name[a.get("name")] = a
    attachments = list(by_name.values())

    if paperclip:
        st.session_state.staged_attachments = attachments
        st.session_state.need_file = False

    if not user_text and not attachments:
        st.stop()

    if not user_text:
        user_text = "(see attached files)"

    # Slash / NL shortcuts → Bulk Enrich page
    _low = user_text.lower().strip()
    import re as _re

    def _shortcut_reply(content: str, routing: str) -> None:
        st.session_state.messages.append({"role": "user", "content": user_text})
        st.session_state.messages.append(
            {"role": "assistant", "content": content, "meta": {"routing": routing}}
        )
        st.session_state.run_active = False
        try:
            durable_store.save_chat_messages(st.session_state.messages)
        except Exception:
            pass
        st.stop()

    if _low.startswith("/enrich"):
        rest = user_text[len("/enrich") :].strip(" :,-")
        st.session_state.bulk_companies = rest.replace(",", "\n")
        _shortcut_reply(
            "Prefilled enrichment list. Go to **🚀 Bulk Enrich & Draft** and click Start Enrichment.",
            "ENRICH_SHORTCUT",
        )
    if _low.startswith("/draft"):
        _shortcut_reply(
            "Use **🚀 Bulk Enrich & Draft** → approve rows → Start Drafting (Phase 2).",
            "DRAFT_SHORTCUT",
        )
    if _low.startswith("/style-refresh"):
        with st.spinner("Refreshing style profile…"):
            from core.style_profile import build_style_profile

            profile = build_style_profile()
        _shortcut_reply(
            f"Style profile refreshed ({profile.get('sample_count', 0)} samples).",
            "STYLE_REFRESH",
        )
    _em = _re.match(
        r"(?i)^\s*enrich(?:\s+these)?\s*:\s*(.+)$",
        user_text,
        flags=_re.S,
    )
    if _em:
        names = [x.strip() for x in _re.split(r"[,;\n]+", _em.group(1)) if x.strip()]
        st.session_state.bulk_companies = "\n".join(names)
        _shortcut_reply(
            f"Prefilled **{len(names)}** orgs for Phase 1. Open **🚀 Bulk Enrich & Draft** to start.",
            "ENRICH_NL",
        )

    file_names = [a.get("name") or "file" for a in attachments]

    st.session_state.run_cancel = False
    st.session_state.run_active = True
    st.session_state.show_edit = False

    st.session_state.messages.append(
        {"role": "user", "content": user_text, "files": file_names}
    )
    with st.chat_message("user"):
        st.markdown(user_text)
        if file_names:
            st.caption("Using files: " + ", ".join(file_names))

    history = [
        {"role": m["role"], "content": m["content"]}
        for m in st.session_state.messages[:-1]
    ]

    def _cancel_check() -> bool:
        return bool(st.session_state.get("run_cancel"))

    with st.chat_message("assistant"):
        status = st.status(
            "Running… use **⏹ Stop** next to the message box to cancel",
            expanded=False,
        )
        placeholder = st.empty()
        text = ""
        meta: dict = {}
        stopped = False
        try:
            for chunk in answer(
                user_text,
                history=history,
                context={
                    "prospects": st.session_state.get("last_prospects") or [],
                    "attachments": attachments,
                    "mailbox_messages": st.session_state.get("last_mailbox") or [],
                    "cancel_check": _cancel_check,
                },
            ):
                if _cancel_check():
                    text += (
                        "\n\n_(⏹ Stopped by you — use **Edit** under the chat box to revise.)_"
                    )
                    stopped = True
                    meta = {**(meta or {}), "cancelled": True, "routing": "STOPPED"}
                    break
                if isinstance(chunk, dict) and "__meta__" in chunk:
                    incoming = chunk["__meta__"] or {}
                    prev_prospects = meta.get("prospects")
                    meta = {**meta, **incoming}
                    if not meta.get("prospects") and prev_prospects:
                        meta["prospects"] = prev_prospects
                else:
                    text += str(chunk)
                    placeholder.markdown(text + "▌")
            placeholder.markdown(text or "_(no response)_")
            if meta.get("draft_debug"):
                st.session_state["draft_debug"] = meta["draft_debug"]
                with st.expander("Debug · draft recipients", expanded=False):
                    _d = meta["draft_debug"]
                    st.write(f"user_message: `{_d.get('user_message') or ''}`")
                    st.write("parsed_directives:")
                    st.json(_d.get("parsed_directives") or {})
                    st.write(f"recipients_final: `{_d.get('recipients_final')}`")
                    st.write(f"draft_path: `{_d.get('draft_path') or '—'}`")
                    st.write(
                        f"ignored_count (session prospects not drafted): "
                        f"`{_d.get('ignored_count')}`"
                    )
            previews = meta.get("draft_previews") or []
            if previews:
                try:
                    from gmail_client.html_format import render_draft_html

                    for pv in previews:
                        st.markdown(
                            render_draft_html(
                                pv.get("subject") or "",
                                pv.get("to") or "",
                                pv.get("cc") or "",
                                pv.get("body_cleaned") or "",
                            ),
                            unsafe_allow_html=True,
                        )
                except Exception:
                    pass
            if stopped or meta.get("cancelled"):
                status.update(label="Stopped", state="error")
            else:
                status.update(label="Done", state="complete")
        except Exception as e:
            status.update(label="Error", state="error")
            text = (text or "") + f"\n\n[error] {e}"
            placeholder.markdown(text)

        if meta.get("mailbox_messages"):
            st.session_state.last_mailbox = meta["mailbox_messages"]
            st.caption(f"Saved {len(meta['mailbox_messages'])} mailbox rows for follow-ups.")

        # Persist ZoomInfo / contact-search hits into Prospects → Saved
        prospects = list(meta.get("prospects") or [])
        try:
            from agent.intent import parse_contact_search_company, wants_contact_search
            from core.prospect_parse import parse_prospects_from_agent_text

            if wants_contact_search(user_text) or str(meta.get("routing") or "").startswith(
                "PROSPECT_SEARCH"
            ):
                company = parse_contact_search_company(user_text) or ""
                parsed = parse_prospects_from_agent_text(
                    text, default_company=company, default_source="zoominfo"
                )
                if parsed:
                    if not prospects:
                        prospects = parsed
                    else:
                        seen = {
                            (p.get("email") or p.get("name") or "").strip().lower()
                            for p in prospects
                        }
                        for p in parsed:
                            key = (p.get("email") or p.get("name") or "").strip().lower()
                            if key and key not in seen:
                                prospects.append(p)
                                seen.add(key)
        except Exception:
            pass

        if prospects:
            st.session_state.last_prospects = prospects
            with_email = sum(1 for p in prospects if (p.get("email") or "").strip())
            try:
                from core.prospect_list import save_prospects_to_drive

                save_status = save_prospects_to_drive(prospects)
                list_n = int(save_status.get("upserted") or 0)
                total_n = int(save_status.get("total") or 0)
                st.success(
                    f"Saved **{list_n or len(prospects)}** contacts to "
                    f"**Prospects → Saved** ({with_email} with email · "
                    f"list total **{total_n or '?'}**)."
                )
            except Exception as e:
                st.caption(
                    f"Session has {len(prospects)} prospects "
                    f"({with_email} with email) — Drive list save failed: {e}"
                )

        sources = meta.get("sources") or []
        if sources:
            with st.expander(f"🔗 Sources ({len(sources)})"):
                for s in sources:
                    title = s.get("title") or s.get("url") or "source"
                    url = s.get("url") or ""
                    stype = s.get("type") or "web"
                    if url:
                        st.markdown(f"- **{title}** ({stype}) — {url}")
                    else:
                        st.markdown(f"- **{title}** ({stype})")
        if meta.get("routing"):
            st.caption(f"Routing: `{meta['routing']}`")

    st.session_state.messages.append(
        {"role": "assistant", "content": text, "meta": meta}
    )
    st.session_state.run_active = False
    st.session_state.run_cancel = False
    try:
        durable_store.save_chat_messages(st.session_state.messages)
        durable_store.save_session_extras(
            prospects=st.session_state.get("last_prospects") or [],
            mailbox=st.session_state.get("last_mailbox") or [],
        )
    except Exception:
        pass

    if meta.get("need_file"):
        st.session_state.need_file = True
        st.session_state.pending_user_msg = meta.get("pending_user_msg") or user_text
        st.rerun()

    if meta.get("consumed_attachments"):
        st.session_state.staged_attachments = []
        st.session_state.need_file = False
        st.session_state.pending_user_msg = ""
        st.rerun()
