# NOTE: Home page only; multipage nav comes from Streamlit pages/ auto-discovery.
from __future__ import annotations

import streamlit as st

from config import APP_NAME

st.set_page_config(page_title=APP_NAME, page_icon="🧠", layout="wide")

st.title(f"🧠 {APP_NAME}")
st.caption("Prospect → Enrich → Schedule → Track → Learn")

c1, c2, c3 = st.columns(3)
c1.metric("Providers", "Apollo + ZoomInfo + RocketReach")
c2.metric("LLM", "Gemini 2.0 Flash")
c3.metric("Search", "Native Google grounding")

st.markdown(
    f"""
### How to use {APP_NAME}

1. **💬 Chat** — Ask research questions; Gemini searches Google natively and cites sources.
2. **🎯 Prospects** — Search Apollo / ZoomInfo / RocketReach, enrich contacts, save to memory.
3. **📅 Schedule** — Queue single, bulk, or drip-sequence emails via Apps Script (works offline).
4. **📬 Tracking** — Opens, clicks, hot leads, and reply auto-pause from the shared Sheet.
5. **📥 Inbox Extract** — Pull structured CRM fields out of recent Gmail threads.
6. **🧠 Memory** — Local Chroma RAG over notes and saved prospects.

Fill `.env` from `.env.example`, deploy Apps Script + Netlify tracking, then start researching.
"""
)
