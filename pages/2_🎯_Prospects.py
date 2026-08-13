# NOTE: Search / Enrich / Saved list — saved contacts persist via prospect_list + Drive.
from __future__ import annotations

import streamlit as st

from config import APP_NAME
from connectors.ingest_to_memory import ingest_prospects, prospects_to_dataframe
from connectors.prospects import enrich_fallthrough, search_all
from core.auth_ui import logout_button, require_login
from core.auto_sync import auto_ingest_prospects, ensure_session_sync
from core.prospect_list import (
    all_prospects,
    reload_from_drive,
    repair_saved_prospects,
    save_prospects,
    search_saved,
    visible_prospects,
)

st.set_page_config(page_title=f"Prospects · {APP_NAME}", page_icon="🎯", layout="wide")
if not require_login():
    st.stop()
logout_button()

# Persist Chat ZoomInfo hits into Saved on every visit (idempotent merge).
_session_hits = [
    p
    for p in (st.session_state.get("last_prospects") or [])
    if p and not p.get("error") and not p.get("research_only")
]
if _session_hits:
    try:
        n_merged = save_prospects(_session_hits)
        if n_merged:
            st.toast(f"Saved **{n_merged}** contacts from Chat/Search into the list")
    except Exception as e:
        st.warning(f"Could not merge session contacts into Saved: {e}")

ensure_session_sync(st.session_state)

# After each deploy Render disk is empty — pull Drive before any save can clobber it
if not st.session_state.get("_prospects_drive_hydrated"):
    st.session_state._prospects_drive_hydrated = True
    try:
        n_cloud = reload_from_drive()
        if n_cloud:
            st.toast(f"Restored **{n_cloud}** contacts from Google Drive")
    except Exception:
        pass
    # Re-apply session hits after Drive reload (stale cloud must not win)
    if _session_hits:
        try:
            save_prospects(_session_hits)
        except Exception:
            pass

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
    "Search results auto-save to your **prospect list** on Google Drive. "
    "Contacts and chat memory survive redeploys. "
    "Missing email triggers ZoomInfo automatically (say **refresh** to force a full re-search)."
)

try:
    from core.drive_store import memory_status

    ms = memory_status()
    if not ms.get("drive_ok"):
        st.error(
            "Google Drive is not connected (token missing `drive.file` or "
            "`BOOTSTRAP_TOKEN_JSON`). Prospects only live on ephemeral disk and "
            "will vanish on the next deploy."
        )
    elif not ms.get("folder_pinned"):
        st.warning(
            "Set **RELAY_DRIVE_FOLDER_ID** on Render to your existing "
            f"`Relay Memory` folder id (`{ms.get('folder_id') or 'unknown'}`). "
            "Without it, a new deploy can attach to an empty Drive folder and "
            "your contacts look gone."
        )
    elif ms.get("prospects_count", 0) == 0 and not ms.get("has_prospects_file"):
        st.info(
            "Drive folder is pinned but `relay_prospects.json` is missing there. "
            "If you had contacts before, open Google Drive, find the older "
            "`Relay Memory` folder that still has that file, and pin its id."
        )
except Exception:
    pass

n_saved = 0
try:
    n_saved = len(
        visible_prospects(session_prospects=st.session_state.get("last_prospects"))
    )
except Exception:
    try:
        n_saved = len(all_prospects())
    except Exception:
        n_saved = 0
if n_saved:
    st.caption(f"Saved on your list: **{n_saved}** contacts")
elif st.session_state.get("_prospects_restored_n") == 0:
    st.caption("Saved list is empty after Drive restore — check the warning above.")

tab_saved, tab_search, tab_enrich = st.tabs(["Saved", "Search", "Enrich"])

with tab_saved:
    st.caption("Contacts saved from ZoomInfo / Chat / Enrich — search and export anytime.")
    c1, c2, c3 = st.columns([2, 2, 1])
    q_name = c1.text_input("Search by name / email / title", key="saved_name")
    q_org = c2.text_input("Search by organisation", key="saved_org")
    if c3.button("Refresh list", use_container_width=True, key="saved_refresh"):
        # Force reload from Drive, then re-merge session Chat hits
        try:
            n = reload_from_drive()
            hits = [
                p
                for p in (st.session_state.get("last_prospects") or [])
                if p and not p.get("error")
            ]
            if hits:
                save_prospects(hits)
            st.toast(f"Loaded {n} contacts from Drive")
        except Exception as e:
            st.warning(f"Drive reload failed: {e}")
        st.rerun()

    try:
        if (q_name or "").strip() or (q_org or "").strip():
            # Filter the union of durable + session so Chat hits aren't hidden
            saved_rows = visible_prospects(
                session_prospects=st.session_state.get("last_prospects")
            )
            name_n = (q_name or "").strip().lower()
            org_n = (q_org or "").strip().lower()
            filtered = []
            for p in saved_rows:
                blob = " ".join(
                    str(p.get(k) or "")
                    for k in ("name", "first_name", "last_name", "email", "title")
                ).lower()
                org_blob = " ".join(
                    str(p.get(k) or "")
                    for k in ("company", "organization", "org", "org_website", "website")
                ).lower()
                if name_n and name_n not in blob:
                    continue
                if org_n and org_n not in org_blob and org_blob not in org_n:
                    continue
                filtered.append(p)
            saved_rows = filtered
        else:
            saved_rows = visible_prospects(
                session_prospects=st.session_state.get("last_prospects")
            )
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
            "No saved contacts yet. In **Chat**, run "
            "`search contacts from sterlite tech` — you should see a green "
            "**Saved to Prospects → Saved** banner, then refresh this tab."
        )
    else:
        _PLACEHOLDER_EMAILS = {"a@b.com", "test@test.com", "example@example.com"}
        placeholders = [
            p
            for p in saved_rows
            if (p.get("email") or "").strip().lower() in _PLACEHOLDER_EMAILS
        ]
        real_rows = [
            p
            for p in saved_rows
            if (p.get("email") or "").strip().lower() not in _PLACEHOLDER_EMAILS
        ]
        if placeholders and not real_rows:
            st.warning(
                "Only a placeholder test contact is on the list "
                f"(`{placeholders[0].get('email')}`). "
                "Chat search results were not saved — run the Sterlite search again "
                "after this deploy and look for the green save banner in Chat."
            )
            if st.button("Remove placeholder test contact", key="del_placeholders"):
                try:
                    from core import prospect_list as pl

                    keep = [
                        p
                        for p in pl.all_prospects()
                        if (p.get("email") or "").strip().lower()
                        not in ("a@b.com", "test@test.com", "example@example.com")
                    ]
                    pl._persist(keep)
                    st.rerun()
                except Exception as e:
                    st.warning(f"Could not delete placeholder: {e}")
        saved_rows = real_rows if real_rows else saved_rows

        if not real_rows and placeholders:
            pass  # warning already shown
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
        st.caption("Provider: **ZoomInfo** only")
        limit = st.slider("Limit", 5, 100, 50)
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
        with st.spinner("Searching ZoomInfo…"):
            results = search_all(
                query, providers=["zoominfo"], limit_per_provider=limit
            )
        st.session_state.last_prospects = results
        clean = [p for p in results if not p.get("error")]
        list_n = 0
        try:
            from core.prospect_list import save_prospects

            list_n = save_prospects(clean)
        except Exception as e:
            st.warning(f"Drive prospect list save failed: {e}")
        saved = auto_ingest_prospects(clean)
        try:
            from core import durable_store

            durable_store.save_session_extras(prospects=results)
        except Exception:
            pass
        st.success(
            f"Got {len(results)} rows. Auto-saved **{list_n or len(saved)}** "
            f"contacts to your Drive prospect list."
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
            ["zoominfo"],
            default=["zoominfo"],
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
                ident, order=order or ["zoominfo"]
            )
        st.session_state.last_enrich = result
        st.json(result)
        if result and not result.get("error"):
            auto_ingest_prospects([result])
            st.caption("Auto-saved enriched contact to your list.")
    elif st.session_state.get("last_enrich"):
        st.json(st.session_state.last_enrich)
