# NOTE: Mailbox prefs (Gmail signature mode). Saved once, reused like Chat model.
from __future__ import annotations

import json
import os
import sys
from typing import Any, Optional

from config import _DATA

MODES = ("gmail", "none")
DEFAULT_MODE = "gmail"
_PREF_FILE = _DATA / "mail_prefs.json"
_cached: Optional[str] = None


def normalize_mode(name: str | None) -> str:
    raw = (name or "").strip().lower()
    if raw in ("gmail", "account", "send-as", "sendas", "include"):
        return "gmail"
    if raw in ("none", "off", "skip", "already"):
        return "none"
    if raw in MODES:
        return raw
    return DEFAULT_MODE


def signature_mode() -> str:
    global _cached
    if _cached in MODES:
        return _cached
    try:
        data = json.loads(_PREF_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("signature_mode"):
            got = normalize_mode(str(data.get("signature_mode") or ""))
            _cached = got
            return got
    except Exception:
        pass
    try:
        from core.durable_store import load_session_extras

        extras = load_session_extras(allow_sheets=False) or {}
        if extras.get("signature_mode"):
            got = normalize_mode(str(extras.get("signature_mode") or ""))
            _cached = got
            return got
    except Exception:
        pass
    env = os.getenv("MAIL_SIGNATURE_MODE") or ""
    if env:
        got = normalize_mode(env)
        _cached = got
        return got
    _cached = DEFAULT_MODE
    return DEFAULT_MODE


def include_gmail_signature() -> bool:
    return signature_mode() == "gmail"


def save_signature_mode(name: str) -> str:
    global _cached
    got = normalize_mode(name)
    _cached = got
    try:
        payload: dict[str, Any] = {}
        if _PREF_FILE.is_file():
            try:
                raw = json.loads(_PREF_FILE.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    payload = raw
            except Exception:
                payload = {}
        payload["signature_mode"] = got
        _PREF_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PREF_FILE.write_text(json.dumps(payload), encoding="utf-8")
    except Exception as e:
        print(f"[mail_prefs] local save failed: {e}", file=sys.stderr)
    try:
        from core.durable_store import load_session_extras, save_json_blob_async

        extras = load_session_extras(allow_sheets=False) or {}
        if not isinstance(extras, dict) or not extras:
            extras = load_session_extras(allow_sheets=True) or {}
        if not isinstance(extras, dict):
            extras = {}
        if extras.get("signature_mode") != got:
            extras = dict(extras)
            extras["signature_mode"] = got
            save_json_blob_async("session_extras", extras)
    except Exception as e:
        print(f"[mail_prefs] drive save failed: {e}", file=sys.stderr)
    return got


def hydrate_into(session_state: Any) -> str:
    got = signature_mode()
    current = session_state.get("mail_signature_mode")
    if current:
        session_state["mail_signature_mode"] = normalize_mode(current)
    else:
        session_state["mail_signature_mode"] = got
    return session_state["mail_signature_mode"]


def reset_cache() -> None:
    global _cached
    _cached = None


def render_sidebar_signature_pref() -> str:
    """Left-pane control. Choice is saved and reused; does not prompt again."""
    import streamlit as st

    hydrate_into(st.session_state)
    picked = st.radio(
        "Gmail signature",
        ["gmail", "none"],
        format_func=lambda k: (
            "Include Gmail signature"
            if k == "gmail"
            else "Don't add (already in Gmail)"
        ),
        key="mail_signature_mode",
        help=(
            "Include Gmail signature uses the signature already configured on "
            "your Gmail send-as account, once. Don't add leaves it out so Relay "
            "does not insert another copy."
        ),
    )
    if picked != signature_mode():
        save_signature_mode(picked)
    if picked == "gmail":
        st.caption("Uses your Gmail account signature once. Not stacked on save.")
    else:
        st.caption("Relay will not insert a signature block.")
    return picked
