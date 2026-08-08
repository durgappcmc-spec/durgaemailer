# NOTE: Extraction uses Gemini JSON mode; large batches burn quota quickly.
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from config import APP_NAME
from core import memory
from gmail_client.extract import extract_batch

st.set_page_config(page_title=f"Inbox Extract · {APP_NAME}", page_icon="📥", layout="wide")
st.title("📥 Inbox Extract")

query = st.text_input("Gmail query", value="newer_than:7d category:primary")
max_results = st.slider("Max results", 1, 50, 10)

if st.button("🔍 Extract"):
    with st.spinner("Reading Gmail + extracting…"):
        results = extract_batch(query, max_results=max_results)
    st.session_state.inbox_extract = results

results = st.session_state.get("inbox_extract") or []
if results:
    rows = []
    for r in results:
        ex = r.get("extracted") or {}
        rows.append(
            {
                "subject": r.get("subject"),
                "from": r.get("from"),
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
        "inbox_extract.csv",
        "text/csv",
    )
    if st.button("💾 Save all to memory"):
        for r in results:
            text = json.dumps(r, default=str)
            memory.add(
                texts=text,
                source="gmail_extract",
                source_id=r.get("message_id"),
                title=r.get("subject") or "email",
                metadata={"from": r.get("from") or ""},
            )
        st.success(f"Saved {len(results)} emails to memory.")
