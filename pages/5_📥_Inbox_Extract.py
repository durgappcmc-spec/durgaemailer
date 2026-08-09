# NOTE: Extraction uses Gemini JSON mode; large AI batches burn quota quickly.
from __future__ import annotations

import pandas as pd
import streamlit as st

from config import APP_NAME
from core.auth_ui import logout_button, require_login
from core.auto_sync import ingest_mailbox_messages
from gmail_client.extract import extract_batch, extract_inbox_and_sent

st.set_page_config(page_title=f"Inbox Extract · {APP_NAME}", page_icon="📥", layout="wide")
if not require_login():
    st.stop()
logout_button()

st.title("📥 Inbox & Sent Extract")
st.caption(
    "Gmail auto-syncs on Home/Chat open and saves emails + contacts to memory. "
    "Use this page for a deeper/manual pull (custom query or AI extract)."
)

mode = st.radio(
    "Source",
    ["Inbox + Sent", "Inbox only", "Sent only", "Custom Gmail query"],
    horizontal=True,
)
days = st.slider("Days window", 1, 365, 30)
max_per = st.slider("Max per mailbox / query", 1, 500, 50)
ai_extract = st.toggle(
    "AI structured extract (uses Gemini quota)",
    value=False,
    help="Off = fast metadata + body snippet. On = sender/company/phones/actions via Gemini.",
)

if mode == "Custom Gmail query":
    query = st.text_input("Gmail query", value=f"newer_than:{days}d")
else:
    query = ""

col_a, col_b = st.columns(2)
run = col_a.button("🔍 Extract", type="primary")
if col_b.button("🗑 Clear results"):
    st.session_state.inbox_extract = []
    st.rerun()

if run:
    with st.spinner("Reading Gmail…"):
        if mode == "Custom Gmail query":
            results = extract_batch(query, max_results=max_per, ai_extract=ai_extract)
            for r in results:
                r["mailbox"] = "custom"
        else:
            results = extract_inbox_and_sent(
                days=days,
                max_per_mailbox=max_per,
                ai_extract=ai_extract,
                include_inbox=mode in ("Inbox + Sent", "Inbox only"),
                include_sent=mode in ("Inbox + Sent", "Sent only"),
            )
    st.session_state.inbox_extract = results
    st.success(f"Loaded {len(results)} messages.")

results = st.session_state.get("inbox_extract") or []
if results:
    rows = []
    for r in results:
        ex = r.get("extracted") or {}
        rows.append(
            {
                "mailbox": r.get("mailbox"),
                "subject": r.get("subject"),
                "from": r.get("from"),
                "date": r.get("date"),
                "sender_name": ex.get("sender_name"),
                "sender_company": ex.get("sender_company"),
                "phones": ", ".join(ex.get("phone_numbers") or []),
                "actions": "; ".join(ex.get("action_items") or []),
                "summary": ex.get("summary"),
            }
        )
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True)
    st.download_button(
        "⬇ CSV",
        df.to_csv(index=False).encode("utf-8"),
        "mailbox_extract.csv",
        "text/csv",
    )
    if st.button("💾 Save all to memory"):
        counts = ingest_mailbox_messages(results)
        st.success(
            f"Saved {counts.get('emails', 0)} emails + "
            f"{counts.get('contacts', 0)} contacts to memory."
        )
