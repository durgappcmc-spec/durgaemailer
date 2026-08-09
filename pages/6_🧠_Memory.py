# NOTE: Manual notes use source="manual"; tags stored as a stringified list for Chroma.
from __future__ import annotations

import streamlit as st

from config import APP_NAME
from core import memory
from core.auth_ui import logout_button, require_login

st.set_page_config(page_title=f"Memory · {APP_NAME}", page_icon="🧠", layout="wide")
if not require_login():
    st.stop()
logout_button()

st.title("🧠 Memory")

q = st.text_input("Search memory")
source = st.selectbox("Source filter", ["all", "gmail_extract", "prospects", "manual"])
k = st.slider("Results", 1, 20, 5)

if q:
    hits = memory.search(q, k=k, source=None if source == "all" else source)
    if not hits:
        st.info("No hits.")
    for hit in hits:
        meta = hit.get("metadata") or {}
        title = meta.get("title") or hit.get("id")
        with st.expander(f"{title} — dist={hit.get('distance')}"):
            st.write(hit.get("text"))
            st.json(meta)

st.divider()
st.subheader("Add note")
title = st.text_input("Title", key="note_title")
tags = st.text_input("Tags (comma-separated)", key="note_tags")
body = st.text_area("Note", height=160, key="note_body")
if st.button("💾 Save note") and body.strip():
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    memory.add(
        texts=body,
        source="manual",
        title=title or "untitled",
        metadata={"tags": tag_list},
    )
    st.success("Saved.")
