# NOTE: Streaming UI captures __meta__ dicts separately from text chunks.
# Files come only from st.chat_input paperclip; staged across turns until cleared/used.
from __future__ import annotations

import streamlit as st

from agent.router import answer
from config import APP_NAME
from core.auth_ui import logout_button, require_login
from gmail_client.attachments import files_to_attachments

st.set_page_config(page_title=f"Chat · {APP_NAME}", page_icon="💬", layout="wide")
if not require_login():
    st.stop()
logout_button()

st.title("💬 Chat")
st.caption(
    "Paperclip = file context/attach. "
    "Try: `show my inbox last 7 days`, `show sent about sponsor`, "
    "then `draft personalized follow-ups to everyone in this list`."
)

if "messages" not in st.session_state:
    st.session_state.messages = []
if "staged_attachments" not in st.session_state:
    st.session_state.staged_attachments = []
if "pending_user_msg" not in st.session_state:
    st.session_state.pending_user_msg = ""
if "need_file" not in st.session_state:
    st.session_state.need_file = False
if "last_mailbox" not in st.session_state:
    st.session_state.last_mailbox = []

staged = st.session_state.staged_attachments or []
mailbox_n = len(st.session_state.get("last_mailbox") or [])
if mailbox_n:
    st.caption(f"Mailbox context loaded: **{mailbox_n}** messages (for filters / follow-ups).")
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
    if c2.button("Clear files", use_container_width=True):
        st.session_state.staged_attachments = []
        st.session_state.need_file = False
        st.session_state.pending_user_msg = ""
        st.rerun()
    pending = (st.session_state.get("pending_user_msg") or "").strip()
    if pending and st.button("Continue pending request", type="primary"):
        st.session_state.force_prompt = pending
        st.session_state.pending_user_msg = ""
        st.session_state.need_file = False
        st.rerun()

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
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

prompt = st.chat_input(
    "Ask anything… paperclip to add files for context / email attach",
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
    if hasattr(prompt, "text"):
        user_text = (prompt.text or "").strip()
        chat_files = list(getattr(prompt, "files", None) or [])
    else:
        user_text = str(prompt).strip()
        chat_files = []

    staged = list(st.session_state.get("staged_attachments") or [])
    paperclip = files_to_attachments(chat_files) if chat_files else []

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

    file_names = [a.get("name") or "file" for a in attachments]

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

    with st.chat_message("assistant"):
        placeholder = st.empty()
        text = ""
        meta: dict = {}
        for chunk in answer(
            user_text,
            history=history,
            context={
                "prospects": st.session_state.get("last_prospects") or [],
                "attachments": attachments,
                "mailbox_messages": st.session_state.get("last_mailbox") or [],
            },
        ):
            if isinstance(chunk, dict) and "__meta__" in chunk:
                meta = chunk["__meta__"]
            else:
                text += str(chunk)
                placeholder.markdown(text + "▌")
        placeholder.markdown(text or "_(no response)_")

        if meta.get("mailbox_messages"):
            st.session_state.last_mailbox = meta["mailbox_messages"]
            st.caption(f"Saved {len(meta['mailbox_messages'])} mailbox rows for follow-ups.")

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

    if meta.get("need_file"):
        st.session_state.need_file = True
        st.session_state.pending_user_msg = meta.get("pending_user_msg") or user_text
        st.rerun()

    if meta.get("consumed_attachments"):
        st.session_state.staged_attachments = []
        st.session_state.need_file = False
        st.session_state.pending_user_msg = ""
        st.rerun()
