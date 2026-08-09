# NOTE: Simple single-user gate. Credentials come from .env (never hardcode in pages).
# Local: leave APP_USERNAME/APP_PASSWORD blank, or set APP_REQUIRE_LOGIN=false.
from __future__ import annotations

import hmac
import os

import streamlit as st

from config import APP_NAME, settings


def _auth_enabled() -> bool:
    flag = (os.getenv("APP_REQUIRE_LOGIN") or "").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    if flag in ("1", "true", "yes", "on"):
        return True
    # Default: only require login when both credentials are configured
    return bool((settings.APP_USERNAME or "").strip() and (settings.APP_PASSWORD or ""))


def require_login() -> bool:
    """Render a login form until authenticated. Returns True when logged in / auth off."""
    if not _auth_enabled():
        st.session_state.authenticated = True
        return True

    if st.session_state.get("authenticated"):
        return True

    st.title(f"🔐 {APP_NAME} Login")
    st.caption("Enter your credentials to continue.")
    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in")

    if submitted:
        user_ok = hmac.compare_digest(
            (username or "").strip(), (settings.APP_USERNAME or "").strip()
        )
        pass_ok = hmac.compare_digest(
            password or "", settings.APP_PASSWORD or ""
        )
        if user_ok and pass_ok:
            st.session_state.authenticated = True
            st.rerun()
        st.error("Invalid username or password.")
    return False


def logout_button() -> None:
    if not _auth_enabled():
        return
    if st.session_state.get("authenticated") and st.sidebar.button("Log out"):
        st.session_state.authenticated = False
        st.rerun()
