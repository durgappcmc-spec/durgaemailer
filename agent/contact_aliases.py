# NOTE: Remember people nicknames → emails for CC/To (Deepti, Raahul, …).
from __future__ import annotations

import re
import sys
from typing import Optional

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Built-in team aliases (always available)
_BUILTIN: dict[str, str] = {
    "deepti": "deepti.87.srivastava@gmail.com",
    "deepti mass communication": "deepti.87.srivastava@gmail.com",
    "deepti srivastava": "deepti.87.srivastava@gmail.com",
    "raahul": "raahul.ppcm@gmail.com",
    "rahul": "raahul.ppcm@gmail.com",
    "raahul chakraborty": "raahul.ppcm@gmail.com",
    "rahul chakraborty": "raahul.ppcm@gmail.com",
}

# Patterns that teach aliases from chat:
#   Raahul as <raahul.ppcm@gmail.com>
#   Deepti <deepti.87.srivastava@gmail.com>
#   CC Deepti (deepti.87.srivastava@gmail.com)
_LEARN_PATTERNS = [
    re.compile(
        r"\b([A-Za-z][A-Za-z .'-]{1,40}?)\s+as\s*<?\s*"
        r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\s*>?",
        re.I,
    ),
    re.compile(
        r"\b([A-Za-z][A-Za-z .'-]{1,40}?)\s*[:=]\s*<?\s*"
        r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\s*>?",
        re.I,
    ),
    re.compile(
        r"\b([A-Za-z][A-Za-z .'-]{1,40}?)\s*[<(]\s*"
        r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\s*[>)]",
        re.I,
    ),
]

_STOP = {
    "cc",
    "to",
    "from",
    "and",
    "or",
    "the",
    "a",
    "an",
    "with",
    "please",
    "also",
    "email",
    "mail",
    "copy",
    "carbon",
    "subject",
    "attach",
    "draft",
    "send",
    "ignore",
    "skip",
    "as",
}

_learned: dict[str, str] = {}
_loaded = False


def _norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _load_learned() -> None:
    global _learned, _loaded
    if _loaded:
        return
    _loaded = True
    try:
        from core.durable_store import load_json_blob

        data = load_json_blob("contact_aliases", allow_sheets=False)
        if data is None:
            data = load_json_blob("contact_aliases", allow_sheets=True)
        if isinstance(data, dict):
            _learned = {
                _norm_name(k): str(v).strip()
                for k, v in data.items()
                if _EMAIL_RE.fullmatch(str(v).strip() or "")
            }
    except Exception as e:
        print(f"[aliases] load failed: {e}", file=sys.stderr)


def _persist_learned() -> None:
    try:
        from core.durable_store import save_json_blob_async

        save_json_blob_async("contact_aliases", dict(_learned))
    except Exception as e:
        print(f"[aliases] save failed: {e}", file=sys.stderr)


def all_aliases() -> dict[str, str]:
    _load_learned()
    out = dict(_BUILTIN)
    out.update(_learned)
    return out


def remember_alias(name: str, email: str) -> bool:
    """Store/override a nickname → email mapping."""
    _load_learned()
    key = _norm_name(name)
    email = (email or "").strip()
    if not key or key in _STOP or not _EMAIL_RE.fullmatch(email):
        return False
    # Skip if already the built-in value
    if _BUILTIN.get(key, "").lower() == email.lower() and key not in _learned:
        return True
    _learned[key] = email
    # Also store first token (Raahul Chakraborty → raahul)
    first = key.split()[0]
    if first and first not in _STOP and len(first) >= 3:
        _learned[first] = email
    _persist_learned()
    return True


def learn_aliases_from_text(text: str) -> list[tuple[str, str]]:
    """Parse 'Name as <email>' style phrases and remember them."""
    learned: list[tuple[str, str]] = []
    msg = text or ""
    for pat in _LEARN_PATTERNS:
        for m in pat.finditer(msg):
            name, email = m.group(1).strip(), m.group(2).strip()
            # Drop trailing junk words from name
            parts = [p for p in re.split(r"[\s,;/]+", name) if p]
            while parts and _norm_name(parts[0]) in _STOP:
                parts.pop(0)
            while parts and _norm_name(parts[-1]) in _STOP:
                parts.pop()
            name = " ".join(parts)
            if remember_alias(name, email):
                learned.append((_norm_name(name), email))
    return learned


def resolve_name(name: str) -> Optional[str]:
    """Return email for a person name/nickname, or None."""
    aliases = all_aliases()
    key = _norm_name(name)
    if not key:
        return None
    if key in aliases:
        return aliases[key]
    # Try first token
    first = key.split()[0]
    if first in aliases:
        return aliases[first]
    # Fuzzy: alias contained in name or vice versa
    for alias, email in aliases.items():
        if len(alias) >= 4 and (alias in key or key in alias):
            return email
    return None


def resolve_names_in_text(text: str) -> list[str]:
    """Find known people mentioned in a CC/To phrase and return their emails."""
    aliases = all_aliases()
    if not text:
        return []
    blob = text or ""
    found: list[str] = []
    seen: set[str] = set()

    # Longer aliases first so "raahul chakraborty" wins over "raahul"
    for alias in sorted(aliases.keys(), key=len, reverse=True):
        if len(alias) < 3:
            continue
        # Word-boundary-ish match
        pat = re.compile(rf"(?<![A-Za-z]){re.escape(alias)}(?![A-Za-z])", re.I)
        if pat.search(blob):
            email = aliases[alias]
            key = email.lower()
            if key not in seen:
                seen.add(key)
                found.append(email)
            # Remove matched span so shorter aliases don't double-count oddly
            blob = pat.sub(" ", blob, count=1)
    return found
