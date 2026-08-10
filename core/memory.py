# NOTE: Embedding model downloads on first use (~80MB all-MiniLM-L6-v2).
# Cloud deploy may omit chromadb; memory then falls back to a JSONL store.
from __future__ import annotations

import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Optional

from config import _DATA, settings

_client = None
_collection = None
_DISABLED = False
_FALLBACK_PATH = Path(settings.CHROMA_DIR).parent / "memory_fallback.jsonl"
if not str(_FALLBACK_PATH).startswith(str(_DATA)):
    _FALLBACK_PATH = _DATA / "memory_fallback.jsonl"
_CLOUD_RESTORED = False


def _restore_memory_from_cloud() -> None:
    """Hydrate local JSONL from Sheets after Render rebuilds wipe /tmp."""
    global _CLOUD_RESTORED
    if _CLOUD_RESTORED:
        return
    _CLOUD_RESTORED = True
    try:
        if _FALLBACK_PATH.exists() and _FALLBACK_PATH.stat().st_size > 0:
            return
        from core.durable_store import load_memory_rows

        rows = load_memory_rows()
        if not rows:
            return
        _FALLBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _FALLBACK_PATH.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"[memory] restored {len(rows)} rows from durable Sheets store", file=sys.stderr)
    except Exception as e:
        print(f"[memory] cloud restore skipped: {e}", file=sys.stderr)


def _sync_memory_to_cloud(existing: dict[str, dict[str, Any]]) -> None:
    try:
        from core.durable_store import save_memory_rows

        save_memory_rows(list(existing.values()))
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
    existing: dict[str, dict[str, Any]] = {}
    try:
        if _FALLBACK_PATH.exists():
            for line in _FALLBACK_PATH.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    existing[str(row.get("id"))] = row
                except Exception:
                    continue
    except Exception:
        existing = {}
    for i, doc_id in enumerate(ids):
        existing[doc_id] = {
            "id": doc_id,
            "text": texts[i],
            "metadata": metadatas[i],
        }
    try:
        _FALLBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _FALLBACK_PATH.open("w", encoding="utf-8") as fh:
            for row in existing.values():
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[memory] fallback write error: {e}", file=sys.stderr)
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
    """Public hook — restore memory JSONL from Sheets after container rebuild."""
    _restore_memory_from_cloud()


def add(
    texts: list[str] | str,
    source: str,
    source_id: Optional[str] = None,
    title: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> list[str]:
    """Upsert one or more texts into memory. Returns ids."""
    if isinstance(texts, str):
        texts = [texts]
    ids: list[str] = []
    metadatas: list[dict[str, Any]] = []
    for i, text in enumerate(texts):
        # Stable id so re-sync overwrites instead of duplicating
        stable = str(source_id or uuid.uuid4().hex)
        doc_id = f"{source}:{stable}" if i == 0 else f"{source}:{stable}:{i}"
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
        ids.append(doc_id)
        metadatas.append(meta)

    col = _get_collection()
    if col is not None:
        try:
            # Prefer upsert so auto-sync / re-search is idempotent
            if hasattr(col, "upsert"):
                col.upsert(documents=texts, ids=ids, metadatas=metadatas)
            else:
                col.add(documents=texts, ids=ids, metadatas=metadatas)
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
