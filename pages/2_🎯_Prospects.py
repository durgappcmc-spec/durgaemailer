# NOTE: "Push to tracker sheet" is intentionally stubbed until Sheet write helpers land.
from __future__ import annotations

import streamlit as st

from config import APP_NAME
from connectors.ingest_to_memory import ingest_prospects, prospects_to_dataframe
from connectors.prospects import enrich_fallthrough, search_all
from core.auth_ui import logout_button, require_login
from core.auto_sync import auto_ingest_prospects, ensure_session_sync

st.set_page_config(page_title=f"Prospects · {APP_NAME}", page_icon="🎯", layout="wide")
if not require_login():
    st.stop()
logout_button()
ensure_session_sync(st.session_state)

st.title("🎯 Prospects")
st.caption("Search results auto-save to memory. ZoomInfo is the default provider.")

tab_search, tab_enrich = st.tabs(["Search", "Enrich"])

with tab_search:
    with st.form("prospect_search"):
        c1, c2 = st.columns(2)
        titles = c1.text_input("Titles (comma-separated)", "VP Marketing, CMO")
        companies = c2.text_input("Companies", "")
        domains = c1.text_input("Company domains", "")
        locations = c2.text_input("Locations", "")
        seniorities = c1.text_input("Seniorities", "vp, director, c_suite")
        keywords = c2.text_input("Keywords", "")
        providers = st.multiselect(
            "Providers",
            ["zoominfo", "apollo", "rocketreach"],
            default=["zoominfo"],
        )
        limit = st.slider("Limit per provider", 5, 100, 50)
        submitted = st.form_submit_button("🔍 Search")

    if submitted:
        query = {
            "titles": titles,
            "company_names": companies,
            "company_domains": domains,
            "locations": locations,
            "seniorities": seniorities,
            "keywords": keywords,
        }
        with st.spinner("Searching providers…"):
            results = search_all(
                query, providers=providers or ["zoominfo"], limit_per_provider=limit
            )
        st.session_state.last_prospects = results
        saved = auto_ingest_prospects([p for p in results if not p.get("error")])
        st.success(
            f"Got {len(results)} rows. Auto-saved **{len(saved)}** contacts to memory."
        )

    prospects = st.session_state.get("last_prospects") or []
    if prospects:
        df = prospects_to_dataframe(prospects)
        st.dataframe(df, use_container_width=True)
        b1, b2, b3 = st.columns(3)
        if b1.button("💾 Re-save to memory"):
            ids = ingest_prospects(prospects)
            st.success(f"Saved {len(ids)} docs to memory.")
        csv = df.to_csv(index=False).encode("utf-8")
        b2.download_button("⬇ Download CSV", csv, "prospects.csv", "text/csv")
        if b3.button("📊 Push to tracker sheet"):
            st.info(
                "Sheet push is not wired yet — export CSV and paste, or connect "
                "GOOGLE_SHEET_ID write helpers in a follow-up."
            )

with tab_enrich:
    with st.form("prospect_enrich"):
        c1, c2 = st.columns(2)
        first = c1.text_input("First name")
        last = c2.text_input("Last name")
        email = c1.text_input("Email")
        company = c2.text_input("Company")
        linkedin = c1.text_input("LinkedIn URL")
        title = c2.text_input("Title")
        order = st.multiselect(
            "Fallthrough order",
            ["zoominfo", "apollo", "rocketreach"],
            default=["zoominfo", "apollo", "rocketreach"],
        )
        go = st.form_submit_button("✨ Enrich")

    if go:
        ident = {
            "first_name": first,
            "last_name": last,
            "email": email,
            "company": company,
            "linkedin_url": linkedin,
            "title": title,
        }
        with st.spinner("Enriching…"):
            result = enrich_fallthrough(
                ident, order=order or ["zoominfo", "apollo", "rocketreach"]
            )
        st.session_state.last_enrich = result
        st.json(result)
        if result and not result.get("error"):
            auto_ingest_prospects([result])
            st.caption("Auto-saved enriched contact to memory.")
    elif st.session_state.get("last_enrich"):
        st.json(st.session_state.last_enrich)
