# NOTE: Search / Enrich / Saved list — saved contacts persist via prospect_list + Drive.
from __future__ import annotations

import streamlit as st

from config import APP_NAME
from connectors.ingest_to_memory import ingest_prospects, prospects_to_dataframe
from connectors.prospects import enrich_fallthrough, search_all
from core.auth_ui import logout_button, require_login
from core.auto_sync import auto_ingest_prospects, ensure_session_sync
from core.prospect_list import all_prospects, repair_saved_prospects, search_saved

st.set_page_config(page_title=f"Prospects · {APP_NAME}", page_icon="🎯", layout="wide")
if not require_login():
    st.stop()
logout_button()
ensure_session_sync(st.session_state)

# One-time repair of name/email mix-ups on the durable list
if not st.session_state.get("_prospects_repaired"):
    st.session_state._prospects_repaired = True
    try:
        n_fix = repair_saved_prospects()
        if n_fix:
            st.toast(f"Fixed {n_fix} contacts where email was stored as name")
    except Exception:
        pass

st.title("🎯 Prospects")
st.caption(
    "Search results auto-save to your **prospect list**. "
    "Asking again reuses contacts that already have email; "
    "missing email triggers ZoomInfo automatically (say **refresh** to force a full re-search)."
)

n_saved = 0
try:
    n_saved = len(all_prospects())
except Exception:
    n_saved = 0
if n_saved:
    st.caption(f"Saved on your list: **{n_saved}** contacts")

tab_saved, tab_search, tab_enrich = st.tabs(["Saved", "Search", "Enrich"])

with tab_saved:
    st.caption("Contacts saved from ZoomInfo / Chat / Enrich — search and export anytime.")
    c1, c2, c3 = st.columns([2, 2, 1])
    q_name = c1.text_input("Search by name / email / title", key="saved_name")
    q_org = c2.text_input("Search by organisation", key="saved_org")
    if c3.button("Refresh list", use_container_width=True, key="saved_refresh"):
        # Force reload from disk/Drive
        import core.prospect_list as pl

        pl._LOADED = False
        pl._CACHE = None
        st.rerun()

    try:
        if (q_name or "").strip() or (q_org or "").strip():
            saved_rows = search_saved(name=q_name, organisation=q_org, limit=1000)
        else:
            saved_rows = all_prospects()
    except Exception as e:
        saved_rows = []
        st.warning(f"Could not load saved contacts: {e}")

    m1, m2, m3 = st.columns(3)
    m1.metric("Showing", len(saved_rows))
    m2.metric(
        "With email",
        sum(1 for p in saved_rows if (p.get("email") or "").strip()),
    )
    m3.metric("Total saved", n_saved)

    if not saved_rows:
        st.info(
            "No saved contacts yet. Run a Search or Enrich, or find orgs in Chat — "
            "they’ll appear here automatically."
        )
    else:
        df_saved = prospects_to_dataframe(saved_rows)
        # Prefer stable column order; drop empty-only cols for readability
        show_cols = [
            c
            for c in [
                "name",
                "title",
                "company",
                "email",
                "phone",
                "mobile",
                "linkedin_url",
                "location",
                "source",
            ]
            if c in df_saved.columns
        ]
        st.dataframe(df_saved[show_cols] if show_cols else df_saved, use_container_width=True)

        fname = "saved_prospects.csv"
        if (q_name or q_org or "").strip():
            fname = "saved_prospects_filtered.csv"
        st.download_button(
            "⬇ Export CSV",
            df_saved.to_csv(index=False).encode("utf-8"),
            fname,
            "text/csv",
            key="saved_csv",
        )
        if st.button("Use these for bulk draft (session)", key="saved_to_session"):
            st.session_state.last_prospects = list(saved_rows)
            st.success(
                f"Loaded **{len(saved_rows)}** contacts into session — "
                f"open Chat and say `draft emails to all these prospects`."
            )

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
        try:
            from core import durable_store

            durable_store.save_session_extras(prospects=results)
        except Exception:
            pass
        st.success(
            f"Got {len(results)} rows. Auto-saved **{len(saved)}** contacts to your list."
        )

    prospects = st.session_state.get("last_prospects") or []
    if prospects:
        df = prospects_to_dataframe(prospects)
        st.dataframe(df, use_container_width=True)
        b1, b2 = st.columns(2)
        if b1.button("💾 Re-save to list"):
            ids = ingest_prospects(prospects)
            st.success(f"Saved {len(ids)} contacts to your list.")
        csv = df.to_csv(index=False).encode("utf-8")
        b2.download_button("⬇ Download CSV", csv, "prospects_search.csv", "text/csv")

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
            st.caption("Auto-saved enriched contact to your list.")
    elif st.session_state.get("last_enrich"):
        st.json(st.session_state.last_enrich)
