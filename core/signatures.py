# NOTE: Per-user email signatures (JSON) + Gmail-compatible signature wrappers.
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_SIG_PATH = _ROOT / "data" / "signatures.json"

SIG_WRAPPER = (
    '<div class="gmail_signature" data-smartmail="gmail_signature" dir="ltr">'
    "{sig_html}</div>"
)
_SIG_BLOCK_RE = re.compile(
    r'<div class="gmail_signature".*?</div>',
    flags=re.I | re.DOTALL,
)

_DEFAULT_HTML = (
    "<p>Best,<br>Durga</p>"
    "<p>Karuna Media<br>"
    "<a href='https://karunamedia.org'>karunamedia.org</a></p>"
)
_SHORT_HTML = "<p>Best,<br>Durga</p>"

_BUILTIN: dict[str, dict[str, str]] = {
    "default": {"name": "Default", "html": _DEFAULT_HTML},
    "short": {"name": "Short", "html": _SHORT_HTML},
    "none": {"name": "None", "html": ""},
}


def _read_store() -> dict[str, Any]:
    try:
        if _SIG_PATH.is_file():
            data = json.loads(_SIG_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _write_store(store: dict[str, Any]) -> None:
    _SIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SIG_PATH.write_text(
        json.dumps(store, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def load_signatures(user_email: str) -> dict[str, dict[str, str]]:
    """Return {sig_id: {name, html}} for this mailbox, with built-in Default/Short/None."""
    email = (user_email or "").strip().lower() or "csr@karunamedia.org"
    store = _read_store()
    raw = store.get(email) if isinstance(store.get(email), dict) else {}
    out = {k: dict(v) for k, v in _BUILTIN.items()}
    try:
        from gmail_client.send import get_signature

        ghtml = (get_signature(email) or "").strip()
        if ghtml:
            out = {
                "gmail": {"name": "Gmail account", "html": ghtml},
                **out,
            }
    except Exception:
        pass
    for sid, row in raw.items():
        if not isinstance(row, dict):
            continue
        key = str(sid or "").strip() or "custom"
        if key == "gmail":
            continue
        out[key] = {
            "name": str(row.get("name") or key.title()),
            "html": str(row.get("html") or ""),
        }
    return out


def save_signature(user_email: str, sig_id: str, *, name: str, html: str) -> None:
    email = (user_email or "").strip().lower() or "csr@karunamedia.org"
    store = _read_store()
    bucket = store.get(email) if isinstance(store.get(email), dict) else {}
    bucket[str(sig_id or "default")] = {"name": name or sig_id, "html": html or ""}
    store[email] = bucket
    _write_store(store)


def get_default_signature_html(user_email: str) -> str:
    sigs = load_signatures(user_email)
    html = str((sigs.get("default") or {}).get("html") or "")
    if html.strip():
        return html
    try:
        from gmail_client.send import get_signature

        gmail_sig = get_signature(user_email) or ""
        if gmail_sig.strip():
            return gmail_sig
    except Exception:
        pass
    return _DEFAULT_HTML


def with_signature(body_html: str, sig_html: str) -> str:
    """Append a Gmail-recognized signature block once."""
    body = body_html or ""
    if not (sig_html or "").strip():
        return body
    if re.search(r'class=["\']gmail_signature["\']', body, re.I):
        return body
    return f"{body}<br>{SIG_WRAPPER.format(sig_html=sig_html)}"


def replace_signature(body_html: str, new_sig_html: str) -> str:
    """Swap or remove the existing gmail_signature block; append if missing."""
    body = body_html or ""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(body, "html.parser")
        block = soup.find("div", class_="gmail_signature")
        if block:
            if not (new_sig_html or "").strip():
                block.decompose()
                return str(soup)
            wrapped = BeautifulSoup(
                SIG_WRAPPER.format(sig_html=new_sig_html), "html.parser"
            )
            replacement = wrapped.find("div", class_="gmail_signature") or wrapped
            block.replace_with(replacement)
            return str(soup)
    except Exception:
        pass
    if _SIG_BLOCK_RE.search(body):
        if not (new_sig_html or "").strip():
            return _SIG_BLOCK_RE.sub("", body).rstrip()
        return _SIG_BLOCK_RE.sub(
            SIG_WRAPPER.format(sig_html=new_sig_html), body, count=1
        )
    return with_signature(body, new_sig_html)
