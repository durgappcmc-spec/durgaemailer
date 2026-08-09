# NOTE: Attachment helpers + light document text extraction for email context.
from __future__ import annotations

import base64
import sys
from typing import Any


def files_to_attachments(uploaded_files: list[Any] | None) -> list[dict[str, Any]]:
    """Convert Streamlit UploadedFile list to send/schedule attachment dicts."""
    out: list[dict[str, Any]] = []
    for f in uploaded_files or []:
        data = f.getvalue()
        name = getattr(f, "name", None) or "file"
        mime = getattr(f, "type", None) or "application/octet-stream"
        item: dict[str, Any] = {
            "name": name,
            "data": data,
            "mime_type": mime,
            "data_base64": base64.b64encode(data).decode("ascii"),
            "mimeType": mime,
        }
        text = extract_file_text(name, data, mime)
        if text:
            item["extracted_text"] = text
        out.append(item)
    return out


def extract_file_text(name: str, data: bytes, mime: str = "") -> str:
    """Extract plain text from PDF / text-like uploads for LLM email context."""
    lower = (name or "").lower()
    mime = (mime or "").lower()
    try:
        if lower.endswith(".pdf") or "pdf" in mime:
            return _extract_pdf_text(data)
        if lower.endswith((".txt", ".md", ".csv", ".json", ".html", ".htm", ".log")) or mime.startswith(
            "text/"
        ):
            return data.decode("utf-8", errors="replace")
        # Best-effort UTF-8 for unknown small text-ish blobs
        if len(data) < 200_000 and b"\x00" not in data[:1000]:
            sample = data.decode("utf-8", errors="ignore")
            if sample.strip() and sum(c.isprintable() or c.isspace() for c in sample) / max(
                len(sample), 1
            ) > 0.85:
                return sample
    except Exception as e:
        print(f"[attachments] extract failed for {name}: {e}", file=sys.stderr)
    return ""


def _extract_pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception as e:
        print(f"[attachments] pypdf not available: {e}", file=sys.stderr)
        return ""
    import io

    reader = PdfReader(io.BytesIO(data))
    parts: list[str] = []
    for page in reader.pages[:40]:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    text = "\n".join(parts).strip()
    # Cap context size for prompts
    return text[:20000]


def document_context_from_attachments(
    attachments: list[dict[str, Any]] | None,
    *,
    max_chars: int = 18000,
) -> str:
    """Build a labeled context block from attachment extracted_text fields."""
    if not attachments:
        return ""
    chunks: list[str] = []
    used = 0
    for att in attachments:
        text = (att.get("extracted_text") or "").strip()
        if not text:
            continue
        name = att.get("name") or "document"
        header = f"--- Document: {name} ---\n"
        room = max_chars - used - len(header)
        if room <= 0:
            break
        body = text[:room]
        chunks.append(header + body)
        used += len(header) + len(body)
    return "\n\n".join(chunks)
