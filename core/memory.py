# NOTE: Embedding model downloads on first use (~80MB all-MiniLM-L6-v2).
# Cloud deploy may omit chromadb; memory then falls back to a JSONL store.
# Every write is snapshotted locally and auto-uploaded to Google Drive (relay_memory.json).
from __future__ import annotations

import json
import os
import re
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Optional

from config import _DATA, settings

_client = None
_collection = None
# Free Render: never load chromadb / sentence-transformers (OOM)
_light = str(os.getenv("RELAY_LIGHT_MEMORY", "true")).strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
_DISABLED = _light
_FALLBACK_PATH = Path(settings.CHROMA_DIR).parent / "memory_fallback.jsonl"
if not str(_FALLBACK_PATH).startswith(str(_DATA)):
    _FALLBACK_PATH = _DATA / "memory_fallback.jsonl"
_CLOUD_RESTORED = False
_MAX_FALLBACK_ROWS = 1200 if _light else 5000

_SYNC_LOCK = threading.Lock()
_PENDING_DRIVE_ROWS: Optional[list[dict[str, Any]]] = None
_DRIVE_FLUSH_TIMER: Optional[threading.Timer] = None
_DRIVE_DEBOUNCE_SEC = 2.0


def _read_fallback_map() -> dict[str, dict[str, Any]]:
    existing: dict[str, dict[str, Any]] = {}
    try:
        if not _FALLBACK_PATH.exists():
            return {}
        for line in _FALLBACK_PATH.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                existing[str(row.get("id"))] = row
            except Exception:
                continue
    except Exception:
        return {}
    return existing


def _write_fallback_map(existing: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    values = list(existing.values())
    if len(values) > _MAX_FALLBACK_ROWS:
        values = values[-_MAX_FALLBACK_ROWS:]
        existing = {str(r.get("id")): r for r in values}
    try:
        _FALLBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _FALLBACK_PATH.open("w", encoding="utf-8") as fh:
            for row in existing.values():
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[memory] fallback write error: {e}", file=sys.stderr)
    return existing


def _restore_memory_from_cloud() -> None:
    """Hydrate local JSONL from Google Drive after rebuilds wipe ephemeral disk."""
    global _CLOUD_RESTORED
    if _CLOUD_RESTORED:
        return
    _CLOUD_RESTORED = True
    try:
        from core.durable_store import load_memory_rows

        local_rows = list(_read_fallback_map().values())
        # Always ask Drive — prefer the richer cloud copy after deploys
        cloud_rows = load_memory_rows(allow_sheets=True) or []
        if not cloud_rows and not local_rows:
            return

        prefer_cloud = bool(
            cloud_rows
            and (
                not local_rows
                or (
                    len(cloud_rows) > max(len(local_rows), 2)
                    and len(local_rows) < max(3, int(len(cloud_rows) * 0.5))
                )
            )
        )
        if not prefer_cloud and local_rows:
            return

        rows = cloud_rows if prefer_cloud else (local_rows or cloud_rows)
        existing = {str(r.get("id")): r for r in rows if isinstance(r, dict) and r.get("id")}
        _write_fallback_map(existing)
        print(
            f"[memory] restored {len(existing)} rows from Google Drive",
            file=sys.stderr,
        )
    except Exception as e:
        print(f"[memory] cloud restore skipped: {e}", file=sys.stderr)


def _flush_pending_drive_upload() -> None:
    """Upload the latest pending memory snapshot to Google Drive."""
    global _PENDING_DRIVE_ROWS, _DRIVE_FLUSH_TIMER
    with _SYNC_LOCK:
        rows = _PENDING_DRIVE_ROWS
        _PENDING_DRIVE_ROWS = None
        _DRIVE_FLUSH_TIMER = None
    if not rows:
        return
    try:
        from core.durable_store import save_memory_rows_async

        save_memory_rows_async(rows)
        print(f"[memory] queued Google Drive save ({len(rows)} rows)", file=sys.stderr)
    except Exception as e:
        print(f"[memory] Drive save failed: {e}", file=sys.stderr)


def flush_memory_to_drive() -> bool:
    """Force-upload current memory to Google Drive immediately."""
    global _DRIVE_FLUSH_TIMER
    with _SYNC_LOCK:
        if _DRIVE_FLUSH_TIMER is not None:
            try:
                _DRIVE_FLUSH_TIMER.cancel()
            except Exception:
                pass
            _DRIVE_FLUSH_TIMER = None
    _flush_pending_drive_upload()
    try:
        existing = _read_fallback_map()
        if not existing:
            return True
        from core.durable_store import save_memory_rows

        return bool(save_memory_rows(list(existing.values())[-_MAX_FALLBACK_ROWS:]))
    except Exception as e:
        print(f"[memory] flush Drive failed: {e}", file=sys.stderr)
        return False


def _sync_memory_to_cloud(existing: dict[str, dict[str, Any]]) -> None:
    """Local durable snapshot + auto Google Drive upload (debounced ~2s)."""
    global _PENDING_DRIVE_ROWS, _DRIVE_FLUSH_TIMER
    try:
        from core.durable_store import save_json_blob

        rows = list(existing.values())[-_MAX_FALLBACK_ROWS:]
        # Instant local durable file (within the container)
        save_json_blob("memory_rows", rows, sync_sheets=False)
        with _SYNC_LOCK:
            _PENDING_DRIVE_ROWS = rows
            if _DRIVE_FLUSH_TIMER is not None:
                try:
                    _DRIVE_FLUSH_TIMER.cancel()
                except Exception:
                    pass
            timer = threading.Timer(_DRIVE_DEBOUNCE_SEC, _flush_pending_drive_upload)
            timer.daemon = True
            _DRIVE_FLUSH_TIMER = timer
            timer.start()
    except Exception as e:
        print(f"[memory] cloud sync skipped: {e}", file=sys.stderr)


def _get_collection():
    global _client, _collection, _DISABLED
    if _DISABLED:
        return None
    if _collection is not None:
        return _collection
    try:
        import chromadb
        from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

        embedder = SentenceTransformerEmbeddingFunction(
            model_name="all-MiniLM-L6-v2"
        )
        _client = chromadb.PersistentClient(path=settings.CHROMA_DIR)
        _collection = _client.get_or_create_collection(
            name="relay",
            embedding_function=embedder,
        )
        return _collection
    except Exception as e:
        print(f"[memory] chroma unavailable, using file fallback ({e})", file=sys.stderr)
        _DISABLED = True
        return None


def _fallback_upsert(
    ids: list[str],
    texts: list[str],
    metadatas: list[dict[str, Any]],
) -> None:
    _restore_memory_from_cloud()
    existing = _read_fallback_map()
    for i, doc_id in enumerate(ids):
        existing[doc_id] = {
            "id": doc_id,
            "text": texts[i],
            "metadata": metadatas[i],
        }
    existing = _write_fallback_map(existing)
    _sync_memory_to_cloud(existing)


def _fallback_search(
    query: str,
    k: int = 5,
    source: Optional[str] = None,
) -> list[dict[str, Any]]:
    _restore_memory_from_cloud()
    rows: list[dict[str, Any]] = []
    try:
        if not _FALLBACK_PATH.exists():
            return []
        for line in _FALLBACK_PATH.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    except Exception as e:
        print(f"[memory] fallback read error: {e}", file=sys.stderr)
        return []

    terms = [t for t in re.split(r"\s+", (query or "").lower()) if t]
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        meta = row.get("metadata") or {}
        if source and source != "all" and meta.get("source") != source:
            continue
        blob = f"{row.get('text') or ''} {json.dumps(meta, default=str)}".lower()
        if not terms:
            score = 1.0
        else:
            score = sum(1.0 for t in terms if t in blob) / len(terms)
        if score > 0:
            scored.append((score, row))
    scored.sort(key=lambda x: x[0], reverse=True)
    hits: list[dict[str, Any]] = []
    for score, row in scored[:k]:
        hits.append(
            {
                "id": row.get("id"),
                "text": row.get("text") or "",
                "metadata": row.get("metadata") or {},
                "distance": 1.0 - float(score),
            }
        )
    return hits


def hydrate_from_cloud() -> None:
    """Public hook — restore memory JSONL from Google Drive after container rebuild."""
    _restore_memory_from_cloud()


def _build_meta(
    source: str,
    source_id: Optional[str],
    title: Optional[str],
    metadata: Optional[dict[str, Any]],
) -> dict[str, Any]:
    meta: dict[str, Any] = {"source": source}
    if source_id:
        meta["source_id"] = str(source_id)
    if title:
        meta["title"] = title
    if metadata:
        for k, v in metadata.items():
            if isinstance(v, (list, dict)):
                meta[k] = str(v)
            elif v is None:
                continue
            else:
                meta[k] = v
    return meta


def add(
    texts: list[str] | str,
    source: str,
    source_id: Optional[str] = None,
    title: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> list[str]:
    """Upsert one or more texts into memory. Returns ids. Auto-saves to Google Drive."""
    if isinstance(texts, str):
        texts = [texts]
    ids: list[str] = []
    metadatas: list[dict[str, Any]] = []
    for i, text in enumerate(texts):
        # Stable id so re-sync overwrites instead of duplicating
        stable = str(source_id or uuid.uuid4().hex)
        doc_id = f"{source}:{stable}" if i == 0 else f"{source}:{stable}:{i}"
        ids.append(doc_id)
        metadatas.append(_build_meta(source, source_id, title, metadata))

    return _upsert_docs(ids, texts, metadatas)


def add_batch(items: list[dict[str, Any]]) -> list[str]:
    """Upsert many independent docs in one JSONL/Drive write (avoids SSL thrash)."""
    if not items:
        return []
    ids: list[str] = []
    texts: list[str] = []
    metadatas: list[dict[str, Any]] = []
    for item in items:
        source = str(item.get("source") or "memory")
        source_id = item.get("source_id")
        title = item.get("title")
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else None
        text = str(item.get("text") or "")
        stable = str(source_id or uuid.uuid4().hex)
        ids.append(f"{source}:{stable}")
        texts.append(text)
        metadatas.append(_build_meta(source, source_id, title, metadata))
    return _upsert_docs(ids, texts, metadatas)


def _upsert_docs(
    ids: list[str],
    texts: list[str],
    metadatas: list[dict[str, Any]],
) -> list[str]:
    col = _get_collection()
    if col is not None:
        try:
            # Prefer upsert so auto-sync / re-search is idempotent
            if hasattr(col, "upsert"):
                col.upsert(documents=texts, ids=ids, metadatas=metadatas)
            else:
                col.add(documents=texts, ids=ids, metadatas=metadatas)
            # Always mirror to Drive-backed JSONL (Render disk is ephemeral)
            _fallback_upsert(ids, texts, metadatas)
            return ids
        except Exception as e:
            print(f"[memory] chroma upsert error, using fallback: {e}", file=sys.stderr)

    _fallback_upsert(ids, texts, metadatas)
    return ids


def search(
    query: str,
    k: int = 5,
    source: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Search memory. Returns list of {id, text, metadata, distance}."""
    col = _get_collection()
    if col is not None:
        where = {"source": source} if source and source != "all" else None
        try:
            kwargs: dict[str, Any] = {"query_texts": [query], "n_results": k}
            if where:
                kwargs["where"] = where
            res = col.query(**kwargs)
            hits: list[dict[str, Any]] = []
            ids = (res.get("ids") or [[]])[0]
            docs = (res.get("documents") or [[]])[0]
            metas = (res.get("metadatas") or [[]])[0]
            dists = (res.get("distances") or [[]])[0]
            for i, doc_id in enumerate(ids):
                hits.append(
                    {
                        "id": doc_id,
                        "text": docs[i] if i < len(docs) else "",
                        "metadata": metas[i] if i < len(metas) else {},
                        "distance": dists[i] if i < len(dists) else None,
                    }
                )
            return hits
        except Exception as e:
            print(f"[memory] chroma search error: {e}", file=sys.stderr)

    return _fallback_search(query, k=k, source=source)


def format_for_prompt(hits: list[dict[str, Any]]) -> str:
    """Compact cited memory block for LLM prompts."""
    if not hits:
        return "(no memory hits)"
    lines: list[str] = []
    for i, hit in enumerate(hits, start=1):
        title = (hit.get("metadata") or {}).get("title", "")
        header = f"[{i}] {title}".strip() if title else f"[{i}]"
        lines.append(f"{header}\n{hit.get('text', '')}")
    return "\n\n".join(lines)
