# NOTE: Uses google-genai SDK (not legacy google-generativeai). Vertex redirect
# URLs in grounding metadata are left as-is; a resolve_urls() helper could be
# added later if citation UX needs final destinations.
from __future__ import annotations

import json
import sys
from typing import Any, Generator, Iterable, Optional

from google import genai
from google.genai import types

from config import settings

_client: Optional[genai.Client] = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        if not settings.GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY is not set")
        _client = genai.Client(api_key=settings.GEMINI_API_KEY)
    return _client


def _history_to_contents(
    history: Optional[list[dict[str, str]]],
) -> list[types.Content]:
    contents: list[types.Content] = []
    if not history:
        return contents
    for msg in history:
        role = msg.get("role", "user")
        # Gemini expects "user" | "model"
        if role == "assistant":
            role = "model"
        if role not in ("user", "model"):
            role = "user"
        text = msg.get("content") or msg.get("text") or ""
        contents.append(
            types.Content(role=role, parts=[types.Part(text=text)])
        )
    return contents


def _openai_messages_to_contents(
    messages: list[dict[str, str]],
) -> list[types.Content]:
    contents: list[types.Content] = []
    for msg in messages:
        role = msg.get("role", "user")
        if role == "assistant":
            role = "model"
        if role == "system":
            # system handled via system_instruction; skip here
            continue
        if role not in ("user", "model"):
            role = "user"
        contents.append(
            types.Content(
                role=role, parts=[types.Part(text=msg.get("content", ""))]
            )
        )
    return contents


def _extract_sources(chunk: Any) -> list[dict[str, str]]:
    """Pull grounding sources from a stream chunk's candidate metadata."""
    sources: list[dict[str, str]] = []
    try:
        candidates = getattr(chunk, "candidates", None) or []
        if not candidates:
            return sources
        meta = getattr(candidates[0], "grounding_metadata", None)
        if not meta:
            return sources
        chunks = getattr(meta, "grounding_chunks", None) or []
        for gc in chunks:
            web = getattr(gc, "web", None)
            if not web:
                continue
            title = getattr(web, "title", "") or ""
            uri = getattr(web, "uri", "") or ""
            # NOTE: vertexaisearch.cloud.google.com redirect URLs left as-is.
            # A resolve_urls() helper could be added later if needed.
            if uri:
                sources.append({"title": title or uri, "url": uri, "type": "web"})
    except Exception as e:
        print(f"[gemini] failed extracting grounding sources: {e}", file=sys.stderr)
    return sources


def chat_grounded(
    user_msg: str,
    history: Optional[list[dict[str, str]]] = None,
    system: Optional[str] = None,
    use_search: bool = True,
    stream: bool = True,
) -> Generator[str | dict[str, Any], None, None]:
    """Main chat with optional Google Search grounding. Yields text chunks,
    then a final {"__meta__": {"sources": [...]}} dict.
    """
    client = _get_client()
    contents = _history_to_contents(history)
    contents.append(
        types.Content(role="user", parts=[types.Part(text=user_msg)])
    )

    tools = None
    if use_search:
        tools = [types.Tool(google_search=types.GoogleSearch())]

    config = types.GenerateContentConfig(
        temperature=0.4,
        tools=tools,
        system_instruction=system,
    )

    sources: list[dict[str, str]] = []
    try:
        if stream:
            stream_resp = client.models.generate_content_stream(
                model=settings.GEMINI_MODEL,
                contents=contents,
                config=config,
            )
            for chunk in stream_resp:
                text = getattr(chunk, "text", None)
                if text:
                    yield text
                extracted = _extract_sources(chunk)
                if extracted:
                    sources = extracted
        else:
            resp = client.models.generate_content(
                model=settings.GEMINI_MODEL,
                contents=contents,
                config=config,
            )
            text = getattr(resp, "text", None) or ""
            if text:
                yield text
            sources = _extract_sources(resp)
    except Exception as e:
        print(f"[gemini] chat_grounded error: {e}", file=sys.stderr)
        yield f"[gemini error] {e}"
        sources = []

    yield {"__meta__": {"sources": sources}}


def chat_fast(
    messages: list[dict[str, str]],
    temperature: float = 0.2,
    max_tokens: int = 500,
    system: Optional[str] = None,
) -> str:
    """Non-grounded fast completion for the router. Returns text directly."""
    client = _get_client()
    system_instruction = system
    if system_instruction is None:
        for msg in messages:
            if msg.get("role") == "system":
                system_instruction = msg.get("content")
                break

    contents = _openai_messages_to_contents(messages)
    if not contents:
        contents = [
            types.Content(role="user", parts=[types.Part(text="")])
        ]

    config = types.GenerateContentConfig(
        temperature=temperature,
        max_output_tokens=max_tokens,
        system_instruction=system_instruction,
    )
    try:
        resp = client.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=contents,
            config=config,
        )
        return (getattr(resp, "text", None) or "").strip()
    except Exception as e:
        print(f"[gemini] chat_fast error: {e}", file=sys.stderr)
        return "CHAT"


def extract_json(
    prompt: str,
    system: Optional[str] = None,
    max_tokens: int = 1000,
) -> str:
    """Structured JSON extraction. Returns raw JSON string."""
    client = _get_client()
    config = types.GenerateContentConfig(
        temperature=0.1,
        max_output_tokens=max_tokens,
        response_mime_type="application/json",
        system_instruction=system,
    )
    try:
        resp = client.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=[
                types.Content(role="user", parts=[types.Part(text=prompt)])
            ],
            config=config,
        )
        return (getattr(resp, "text", None) or "{}").strip()
    except Exception as e:
        print(f"[gemini] extract_json error: {e}", file=sys.stderr)
        return json.dumps({"error": str(e)})
