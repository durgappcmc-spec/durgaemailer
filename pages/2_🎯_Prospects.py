# NOTE: "Push to tracker sheet" is intentionally stubbed until Sheet write helpers land.
from __future__ import annotations

import streamlit as st

from config import APP_NAME
from core.auth_ui import logout_button, require_login
from connectors.ingest_to_memory import ingest_prospects, prospects_to_dataframe
from connectors.prospects import enrich_fallthrough, search_all

st.set_page_config(page_title=f"Prospects · {APP_NAME}", page_icon="🎯", layout="wide")
if not require_login():
    st.stop()
logout_button()

st.title("🎯 Prospects")

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
            ["apollo", "rocketreach", "zoominfo"],
            default=["apollo", "rocketreach"],
        )
        limit = st.slider("Limit per provider", 5, 50, 10)
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
            results = search_all(query, providers=providers or ["apollo"], limit_per_provider=limit)
        st.session_state.last_prospects = results
        st.success(f"Got {len(results)} rows (including any error stubs).")

    prospects = st.session_state.get("last_prospects") or []
    if prospects:
        df = prospects_to_dataframe(prospects)
        st.dataframe(df, use_container_width=True)
        b1, b2, b3 = st.columns(3)
        if b1.button("💾 Save to memory"):
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
            ["rocketreach", "apollo", "zoominfo"],
            default=["rocketreach", "apollo", "zoominfo"],
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
            result = enrich_fallthrough(ident, order=order or ["rocketreach", "apollo"])
        st.session_state.last_enrich = result
        st.json(result)
        if st.button("Save this to memory") and result and not result.get("error"):
            ingest_prospects([result])
            st.success("Saved.")
    elif st.session_state.get("last_enrich"):
        st.json(st.session_state.last_enrich)
