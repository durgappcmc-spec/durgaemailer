# NOTE: Embedding model downloads on first use (~80MB all-MiniLM-L6-v2).
# Cloud deploy may omit chromadb; memory then becomes a no-op.
from __future__ import annotations

import sys
import uuid
from typing import Any, Optional

from config import settings

_client = None
_collection = None
_DISABLED = False


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
        print(f"[memory] disabled ({e})", file=sys.stderr)
        _DISABLED = True
        return None


def add(
    texts: list[str] | str,
    source: str,
    source_id: Optional[str] = None,
    title: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> list[str]:
    """Add one or more texts to the relay collection. Returns ids."""
    if isinstance(texts, str):
        texts = [texts]
    col = _get_collection()
    if col is None:
        return []
    ids: list[str] = []
    metadatas: list[dict[str, Any]] = []
    for i, text in enumerate(texts):
        doc_id = f"{source}:{source_id or uuid.uuid4().hex}:{i}"
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
    try:
        col.add(documents=texts, ids=ids, metadatas=metadatas)
    except Exception as e:
        print(f"[memory] add error: {e}", file=sys.stderr)
    return ids


def search(
    query: str,
    k: int = 5,
    source: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Search memory. Returns list of {id, text, metadata, distance}."""
    col = _get_collection()
    if col is None:
        return []
    where = {"source": source} if source and source != "all" else None
    try:
        kwargs: dict[str, Any] = {"query_texts": [query], "n_results": k}
        if where:
            kwargs["where"] = where
        res = col.query(**kwargs)
    except Exception as e:
        print(f"[memory] search error: {e}", file=sys.stderr)
        return []

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
