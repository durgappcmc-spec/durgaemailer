# NOTE: Home page only; multipage nav comes from Streamlit pages/ auto-discovery.
from __future__ import annotations

import streamlit as st

from config import APP_NAME
from core.auth_ui import logout_button, require_login
from core.auto_sync import ensure_session_sync

st.set_page_config(page_title=APP_NAME, page_icon="🧠", layout="wide")
if not require_login():
    st.stop()
logout_button()

sync = ensure_session_sync(st.session_state)

st.title(f"🧠 {APP_NAME}")
st.caption("Prospect → Enrich → Schedule → Track → Learn")

c1, c2, c3 = st.columns(3)
c1.metric("Providers", "Apollo + ZoomInfo + RocketReach")
c2.metric("LLM", "Gemini")
c3.metric("Search", "Native Google grounding")

if sync.get("ok"):
    st.success(
        f"Auto-synced Gmail: **{sync.get('messages', 0)}** messages → "
        f"**{sync.get('emails', 0)}** emails + **{sync.get('contacts', 0)}** contacts in memory."
    )
elif sync.get("skipped") and sync.get("reason") == "recently synced":
    st.caption(
        f"Gmail auto-sync is fresh "
        f"({sync.get('seconds_ago', 0)}s ago · "
        f"{sync.get('messages', 0)} msgs / {sync.get('contacts', 0)} contacts)."
    )
elif sync.get("error"):
    st.warning(f"Gmail auto-sync skipped: {sync.get('error')}")

st.markdown(
    f"""
### How to use {APP_NAME}

1. **💬 Chat** — Ask research questions; Gemini searches Google natively and cites sources.
2. **🎯 Prospects** — Search ZoomInfo (default) / Apollo / RocketReach; contacts auto-save to memory.
3. **📅 Schedule** — Send/draft now (with attachments), or queue single/bulk/sequence emails.
4. **📬 Tracking** — Opens, clicks, hot leads, and reply auto-pause from the shared Sheet.
5. **📥 Inbox Extract** — Manual deep extract (optional). Gmail auto-syncs on app open.
6. **🧠 Memory** — Chroma / file RAG over notes, ZoomInfo hits, and synced mailbox contacts.
"""
)
