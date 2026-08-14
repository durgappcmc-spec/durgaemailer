# NOTE: Chat page model preference (Gemini vs Genspark). Saved once, reused.
from __future__ import annotations

import json
import os
import sys
from contextvars import ContextVar, Token
from typing import Any, Optional

from config import _DATA

PROVIDERS = ("gemini", "genspark")
DEFAULT_PROVIDER = "gemini"

_ACTIVE: ContextVar[str] = ContextVar("chat_llm_provider", default="")
_PREF_FILE = _DATA / "chat_llm.json"
_cached: Optional[str] = None


def normalize(name: str | None) -> str:
    raw = (name or "").strip().lower()
    if raw in ("gsk", "claude", "genspark.ai"):
        return "genspark"
    if raw in PROVIDERS:
        return raw
    return DEFAULT_PROVIDER


def preferred_provider() -> str:
    """Active Chat run, else last saved choice, else Gemini."""
    active = (_ACTIVE.get() or "").strip()
    if active in PROVIDERS:
        return active
    return load_provider()


def active_provider() -> str:
    """Provider for this Chat `answer()` call only. Empty outside Chat."""
    return (_ACTIVE.get() or "").strip()


def use_provider(name: str) -> Token:
    return _ACTIVE.set(normalize(name))


def reset_provider(token: Token) -> None:
    try:
        _ACTIVE.reset(token)
    except Exception:
        pass


def load_provider() -> str:
    global _cached
    if _cached in PROVIDERS:
        return _cached
    try:
        data = json.loads(_PREF_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            got = normalize(str(data.get("provider") or ""))
            if got in PROVIDERS and data.get("provider"):
                _cached = got
                return got
    except Exception:
        pass
    try:
        from core.durable_store import load_session_extras

        extras = load_session_extras(allow_sheets=False)
        got = normalize(str((extras or {}).get("chat_llm_provider") or ""))
        if extras.get("chat_llm_provider") and got in PROVIDERS:
            _cached = got
            return got
    except Exception:
        pass
    env = normalize(os.getenv("CHAT_LLM_PROVIDER") or "")
    if os.getenv("CHAT_LLM_PROVIDER") and env in PROVIDERS:
        _cached = env
        return env
    _cached = DEFAULT_PROVIDER
    return DEFAULT_PROVIDER


def save_provider(name: str) -> str:
    """Persist the Chat model so later sessions do not ask again."""
    global _cached
    got = normalize(name)
    _cached = got
    payload = {"provider": got}
    try:
        _PREF_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PREF_FILE.write_text(json.dumps(payload), encoding="utf-8")
    except Exception as e:
        print(f"[chat_llm] local save failed: {e}", file=sys.stderr)
    try:
        from core.durable_store import load_session_extras, save_json_blob_async

        extras = load_session_extras(allow_sheets=False) or {}
        if not isinstance(extras, dict) or not extras:
            extras = load_session_extras(allow_sheets=True) or {}
        if not isinstance(extras, dict):
            extras = {}
        if extras.get("chat_llm_provider") != got:
            extras = dict(extras)
            extras["chat_llm_provider"] = got
            save_json_blob_async("session_extras", extras)
    except Exception as e:
        print(f"[chat_llm] drive save failed: {e}", file=sys.stderr)
    return got


def genspark_ready() -> bool:
    try:
        from core.genspark_client import available

        return bool(available())
    except Exception:
        return False


def resolve_chat_provider() -> str:
    """Saved Chat choice, falling back to Gemini if Genspark has no key."""
    want = preferred_provider()
    if want == "genspark" and not genspark_ready():
        return DEFAULT_PROVIDER
    return want


def hydrate_into(session_state: Any) -> str:
    got = load_provider()
    current = session_state.get("chat_llm_provider")
    if current:
        session_state["chat_llm_provider"] = normalize(current)
    else:
        session_state["chat_llm_provider"] = got
    return session_state["chat_llm_provider"]


def reset_cache() -> None:
    global _cached
    _cached = None
