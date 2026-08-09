# NOTE: Streaming UI captures __meta__ dicts separately from text chunks.
# Chat accepts multiple file attachments for draft/send/schedule turns.
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
    "Attach PDFs or text files below (or via the paperclip). Ask to **draft** / "
    "**send** / **schedule** an email — Relay uses the document text as context for "
    "the email body, and also attaches the files to the message."
)

if "messages" not in st.session_state:
    st.session_state.messages = []

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        files = msg.get("files") or []
        if files:
            st.caption("Attached: " + ", ".join(files))
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

# Persistent uploader (works even if chat_input file UI is cleared)
extra_files = st.file_uploader(
    "Files for next email (PDF/text also used as draft context)",
    accept_multiple_files=True,
    key="chat_file_uploader",
)

prompt = st.chat_input(
    "Ask Relay anything… (attach files for email draft/send)",
    accept_file="multiple",
    file_type=None,
)

if prompt:
    # Streamlit >=1.39 may return ChatInputValue with .text / .files
    if hasattr(prompt, "text"):
        user_text = (prompt.text or "").strip()
        chat_files = list(prompt.files or [])
    else:
        user_text = str(prompt).strip()
        chat_files = []

    if not user_text and not chat_files and not extra_files:
        st.stop()

    if not user_text:
        user_text = "(see attached files)"

    uploaded = chat_files + list(extra_files or [])
    attachments = files_to_attachments(uploaded)
    file_names = [a.get("name") or "file" for a in attachments]

    st.session_state.messages.append(
        {"role": "user", "content": user_text, "files": file_names}
    )
    with st.chat_message("user"):
        st.markdown(user_text)
        if file_names:
            st.caption("Attached: " + ", ".join(file_names))

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
            },
        ):
            if isinstance(chunk, dict) and "__meta__" in chunk:
                meta = chunk["__meta__"]
            else:
                text += str(chunk)
                placeholder.markdown(text + "▌")
        placeholder.markdown(text or "_(no response)_")

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
    # Clear uploader for next turn by bumping a nonce key via session flag
    if attachments:
        st.session_state.pop("chat_file_uploader", None)
