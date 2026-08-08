# NOTE: Streaming UI captures __meta__ dicts separately from text chunks.
from __future__ import annotations

import streamlit as st

from agent.router import answer
from config import APP_NAME

st.set_page_config(page_title=f"Chat · {APP_NAME}", page_icon="💬", layout="wide")
st.title("💬 Chat")

if "messages" not in st.session_state:
    st.session_state.messages = []

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
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

prompt = st.chat_input("Ask Relay anything…")
if prompt:
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    history = [
        {"role": m["role"], "content": m["content"]}
        for m in st.session_state.messages[:-1]
    ]
    # Convert assistant→model is handled inside llm; keep OpenAI-style here.

    with st.chat_message("assistant"):
        placeholder = st.empty()
        text = ""
        meta: dict = {}
        for chunk in answer(prompt, history=history):
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
