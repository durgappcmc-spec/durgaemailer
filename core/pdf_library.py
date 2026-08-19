# NOTE: Persistent file library (Files page) — named attach per email in Chat.
from __future__ import annotations

import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from config import _DATA

_LIB_DIR = _DATA / "pdf_library"
_INDEX = "index.json"
_MAX_BYTES = 25 * 1024 * 1024
_GENERIC_QUERIES = frozenset(
    {
        "pdf",
        "file",
        "doc",
        "document",
        "attachment",
        "attachments",
        "the",
        "a",
        "an",
        "upload",
        "this",
        "that",
        "my",
    }
)


def lib_dir() -> Path:
    return _LIB_DIR


def set_lib_dir(path: Path) -> None:
    """Tests can point the library at a temp folder."""
    global _LIB_DIR
    _LIB_DIR = Path(path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _ensure_dir() -> Path:
    d = lib_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _index_path() -> Path:
    return _ensure_dir() / _INDEX


def _read_index() -> list[dict[str, Any]]:
    path = _index_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict) and r.get("id")]
    except Exception as e:
        print(f"[pdf_library] index read failed: {e}", file=sys.stderr)
    return []


def _write_index(rows: list[dict[str, Any]]) -> None:
    _index_path().write_text(
        json.dumps(rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _safe_filename(name: str) -> str:
    base = Path(name or "file").name.strip() or "file"
    base = re.sub(r"[^\w.\- ]+", "_", base).strip(" .") or "file"
    return base[:180]


def _unique_name(name: str, existing: list[dict[str, Any]]) -> str:
    wanted = _safe_filename(name)
    taken = {str(r.get("name") or "").lower() for r in existing}
    if wanted.lower() not in taken:
        return wanted
    stem = Path(wanted).stem
    ext = Path(wanted).suffix
    n = 2
    while True:
        cand = f"{stem} ({n}){ext}"
        if cand.lower() not in taken:
            return cand
        n += 1


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f" {n / 1024:.1f} KB".strip()
    return f"{n / (1024 * 1024):.1f} MB"


def list_files() -> list[dict[str, Any]]:
    """Metadata only (no bytes). Newest last."""
    rows = []
    for row in _read_index():
        stored = lib_dir() / str(row.get("stored_as") or "")
        size = int(row.get("size") or 0)
        if stored.is_file() and not size:
            size = stored.stat().st_size
        rows.append(
            {
                **row,
                "size": size,
                "size_label": _fmt_size(size),
                "path": str(stored),
            }
        )
    return rows


def get_file(file_id: str) -> Optional[dict[str, Any]]:
    pid = (file_id or "").strip()
    if not pid:
        return None
    for row in list_files():
        if str(row.get("id") or "") == pid:
            return row
        if str(row.get("name") or "").lower() == pid.lower():
            return row
    return None


def list_pdfs() -> list[dict[str, Any]]:
    return list_files()


def get_pdf(pdf_id: str) -> Optional[dict[str, Any]]:
    return get_file(pdf_id)


def load_attachment(row: dict[str, Any] | str) -> Optional[dict[str, Any]]:
    """Full attachment dict with bytes + optional extracted text."""
    meta = get_file(row) if isinstance(row, str) else dict(row or {})
    if not meta:
        return None
    stored = lib_dir() / str(meta.get("stored_as") or "")
    if not stored.is_file():
        return None
    data = stored.read_bytes()
    name = str(meta.get("name") or stored.name)
    mime = str(meta.get("mime") or "") or "application/octet-stream"
    text = ""
    sidecar = stored.with_suffix(".txt")
    if sidecar.is_file():
        try:
            text = sidecar.read_text(encoding="utf-8")
        except Exception:
            text = ""
    if not text:
        try:
            from gmail_client.attachments import extract_file_text

            text = extract_file_text(name, data, mime)
            if text:
                sidecar.write_text(text[:20000], encoding="utf-8")
        except Exception as e:
            print(f"[pdf_library] extract failed: {e}", file=sys.stderr)
    return {
        "id": meta.get("id"),
        "name": name,
        "data": data,
        "mime_type": mime,
        "mimeType": mime,
        "size": len(data),
        "extracted_text": text,
        "has_context": bool((text or "").strip()),
        "from_library": True,
    }


def save_uploads(uploaded_files: list[Any] | None) -> list[dict[str, Any]]:
    """Persist Streamlit UploadedFile / file-like objects into the Files library."""
    from gmail_client.attachments import files_to_attachments

    items = files_to_attachments(uploaded_files)
    existing = _read_index()
    saved: list[dict[str, Any]] = []
    for item in items:
        name = str(item.get("name") or "file")
        data = item.get("data")
        if data is None:
            continue
        mime = str(item.get("mime_type") or item.get("mimeType") or "").strip()
        if not mime:
            mime = "application/octet-stream"
        if len(data) > _MAX_BYTES:
            print(
                f"[pdf_library] skip {name}: {len(data)} bytes over {_MAX_BYTES}",
                file=sys.stderr,
            )
            continue
        display = _unique_name(name, existing)
        pid = uuid.uuid4().hex[:12]
        ext = Path(display).suffix.lower() or Path(name).suffix.lower()
        stored_as = f"{pid}{ext}"
        path = _ensure_dir() / stored_as
        path.write_bytes(data)
        text = str(item.get("extracted_text") or "")
        if text:
            path.with_suffix(".txt").write_text(text[:20000], encoding="utf-8")
        row = {
            "id": pid,
            "name": display,
            "stored_as": stored_as,
            "size": len(data),
            "mime": mime,
            "added_at": _now(),
        }
        existing.append(row)
        saved.append(row)
    if saved:
        _write_index(existing)
    return saved


def delete_file(file_id: str) -> bool:
    pid = (file_id or "").strip()
    if not pid:
        return False
    rows = _read_index()
    keep: list[dict[str, Any]] = []
    removed = None
    for row in rows:
        if str(row.get("id") or "") == pid or str(row.get("name") or "") == pid:
            removed = row
            continue
        keep.append(row)
    if not removed:
        return False
    stored = lib_dir() / str(removed.get("stored_as") or "")
    for p in (stored, stored.with_suffix(".txt")):
        try:
            if p.is_file():
                p.unlink()
        except Exception:
            pass
    _write_index(keep)
    return True


def delete_pdf(pdf_id: str) -> bool:
    return delete_file(pdf_id)


def _norm_query(query: str) -> str:
    q = (query or "").strip().lower()
    q = q.strip("\"'`.,;:()[]")
    q = re.sub(r"^(the|a|an|file|pdf|document|attachment)\s+", "", q).strip()
    q = re.sub(r"\s+", " ", q)
    return q


def match_query(query: str, items: Optional[list[dict[str, Any]]] = None) -> list[dict[str, Any]]:
    """Resolve a chat filename / stem to library (or pool) rows."""
    q = _norm_query(query)
    if not q or q in _GENERIC_QUERIES:
        return []
    pool = items if items is not None else list_files()
    if not pool:
        return []

    def _name(row: dict[str, Any]) -> str:
        return str(row.get("name") or "").lower()

    def _stem(row: dict[str, Any]) -> str:
        return Path(_name(row)).stem.lower()

    exact = [r for r in pool if _name(r) == q]
    if exact:
        return exact
    q_stem = Path(q).stem if "." in Path(q).name else q
    stems = [r for r in pool if _stem(r) == q_stem]
    if stems:
        return stems
    if len(q_stem) < 3:
        return []
    subs = [
        r
        for r in pool
        if q_stem in _name(r) or q_stem in _stem(r)
    ]
    if len(subs) == 1:
        return subs
    starts = [r for r in subs if _stem(r).startswith(q_stem)]
    if len(starts) == 1:
        return starts
    return subs


def files_mentioned(
    text: str,
    items: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """Library / pool files whose filename or stem appears in the message."""
    msg = text or ""
    if not msg.strip():
        return []
    pool = items if items is not None else list_files()
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in pool:
        name = str(row.get("name") or "")
        if not name:
            continue
        stem = Path(name).stem
        hit = False
        if re.search(re.escape(name), msg, re.I):
            hit = True
        elif len(stem) >= 4 and re.search(
            r"(?<![A-Za-z0-9])" + re.escape(stem) + r"(?![A-Za-z0-9])",
            msg,
            re.I,
        ):
            hit = True
        if hit:
            key = str(row.get("id") or name).lower()
            if key not in seen:
                seen.add(key)
                found.append(row)
    return found


def _uniq_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        key = str(row.get("id") or row.get("name") or "").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _pool_from_staged(staged: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for a in staged or []:
        name = str(a.get("name") or "")
        if not name:
            continue
        out.append(
            {
                **a,
                "id": a.get("id") or f"staged:{name.lower()}",
                "name": name,
                "from_library": False,
            }
        )
    return out


def _load_row(
    row: dict[str, Any],
    staged: list[dict[str, Any]] | None,
) -> Optional[dict[str, Any]]:
    if row.get("data") is not None or row.get("data_base64"):
        return row
    if row.get("from_library") is False:
        for a in staged or []:
            if str(a.get("name") or "").lower() == str(row.get("name") or "").lower():
                return a
        return row if row.get("data") is not None else None
    return load_attachment(row)


def resolve_message_attachments(
    user_msg: str,
    *,
    staged: Optional[list[dict[str, Any]]] = None,
    named: Optional[list[str]] = None,
    named_by_email: Optional[dict[str, list[str]]] = None,
    wants_attach: bool = False,
    wants_context: bool = False,
) -> dict[str, Any]:
    """Pick library + staged files for this chat turn.

    Returns {
      default: list[attachment dicts] | None,  # everyone, if no per-email map
      by_email: {email: list[attachment dicts]},
      docs: list[attachment dicts],  # for LLM context
      unmatched: list[str],
      available: list[str],
    }
    """
    library = list_files()
    staged_pool = _pool_from_staged(staged)
    pool = staged_pool + library
    unmatched: list[str] = []

    def resolve_names(names: list[str]) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for raw in names or []:
            hits = match_query(raw, pool)
            if not hits:
                unmatched.append(str(raw))
                continue
            for h in hits:
                loaded = _load_row(h, staged)
                if loaded:
                    found.append(loaded)
        return _uniq_rows(found)

    by_email: dict[str, list[dict[str, Any]]] = {}
    for email, names in (named_by_email or {}).items():
        key = str(email or "").strip().lower()
        if not key or not names:
            continue
        files = resolve_names(names)
        if files:
            by_email[key] = files

    named_files = resolve_names(list(named or []))
    mentioned_meta = files_mentioned(user_msg or "", pool)
    mentioned_files: list[dict[str, Any]] = []
    for h in mentioned_meta:
        loaded = _load_row(h, staged)
        if loaded:
            mentioned_files.append(loaded)
    mentioned_files = _uniq_rows(mentioned_files)

    assigned_names = {
        str(a.get("name") or "").lower()
        for files in by_email.values()
        for a in files
    }
    global_named = [
        a
        for a in named_files
        if str(a.get("name") or "").lower() not in assigned_names
    ]
    default = global_named or (
        mentioned_files if not by_email else []
    )
    if not default and not by_email and wants_attach:
        if staged:
            default = list(staged)
        elif len(library) == 1:
            loaded = load_attachment(library[0])
            if loaded:
                default = [loaded]

    docs = _uniq_rows(list(staged or []) + mentioned_files + named_files)
    for files in by_email.values():
        docs = _uniq_rows(docs + files)
    if not docs and wants_context and len(library) == 1:
        loaded = load_attachment(library[0])
        if loaded:
            docs = [loaded]

    return {
        "default": default or None,
        "by_email": by_email,
        "docs": docs,
        "unmatched": unmatched,
        "available": [str(r.get("name") or "") for r in library],
        "library_count": len(library),
    }


def pick_for_recipient(
    email: str,
    *,
    default: Optional[list[dict[str, Any]]],
    by_email: Optional[dict[str, list[dict[str, Any]]]] = None,
) -> Optional[list[dict[str, Any]]]:
    key = (email or "").strip().lower()
    if by_email and key in by_email:
        return by_email[key]
    if by_email and default:
        extra = by_email.get(key) or []
        if extra:
            return _uniq_rows(list(default) + extra)
    return default


def file_icon(name: str) -> str:
    ext = Path(name or "").suffix.lower()
    if ext == ".pdf":
        return "📄"
    if ext in {".doc", ".docx", ".rtf", ".txt", ".md"}:
        return "📝"
    if ext in {".xls", ".xlsx", ".csv"}:
        return "📊"
    if ext in {".ppt", ".pptx"}:
        return "📑"
    if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
        return "🖼️"
    if ext == ".zip":
        return "📦"
    return "📎"


def render_sidebar_pdf_library() -> None:
    """Chat left-rail: names from the Files page (upload lives on Files)."""
    import streamlit as st

    try:
        st.page_link("pages/9_📁_Files.py", label="Open Files", icon="📁")
    except Exception:
        st.caption("Open **📁 Files** in the left nav to upload.")
    rows = list_files()
    if not rows:
        st.caption(
            "No files yet. Upload on **Files**, then say "
            "`attach one-pager.pdf` in chat."
        )
        return
    for row in list(reversed(rows))[:10]:
        name = str(row.get("name") or "file")
        st.caption(f"{file_icon(name)} `{name}`")
    extra = len(rows) - 10
    if extra > 0:
        st.caption(f"+ {extra} more on Files")
