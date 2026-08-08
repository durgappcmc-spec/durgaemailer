# NOTE: Router uses chat_fast with a single-line decision; falls back to CHAT on parse failure.
from __future__ import annotations

import json
import re
import sys
from typing import Any, Generator, Optional

from connectors.ingest_to_memory import prospect_to_text
from connectors.prospects import enrich_fallthrough, search_all
from core import memory as mem
from core.llm import chat_fast, chat_grounded
from gmail_client.extract import extract_batch
from scheduling.client import schedule_email

ROUTER_SYSTEM = """You are a routing classifier for Relay, a prospect research and outreach tool.
Given the user message, output EXACTLY ONE line choosing one of:

CHAT
MEMORY
PROSPECT_SEARCH:<json>
PROSPECT_ENRICH:<json>
GMAIL_EXTRACT:<gmail query>
SCHEDULE_EMAIL:<json>

Rules:
- CHAT: general questions, research, writing help — Gemini will use Google Search.
- MEMORY: user asks about saved notes/prospects already in memory.
- PROSPECT_SEARCH: find people. JSON keys may include titles, company_names, company_domains, locations, seniorities, keywords (comma strings or arrays).
- PROSPECT_ENRICH: enrich one person. JSON keys: first_name, last_name, email, company, linkedin_url, title.
- GMAIL_EXTRACT: pull structured data from inbox; after the colon put a Gmail search query.
- SCHEDULE_EMAIL: schedule a send. JSON: recipient_email, subject, html_body, send_at (ISO), optional recipient_name, campaign.

Output NOTHING except the single routing line.
"""


def _parse_json_tail(routing: str, prefix: str) -> dict[str, Any]:
    """Parse JSON after a routing prefix; recover from markdown fences."""
    tail = routing[len(prefix) :].strip()
    if tail.startswith("```"):
        tail = re.sub(r"^```(?:json)?\s*", "", tail)
        tail = re.sub(r"\s*```$", "", tail)
    try:
        return json.loads(tail)
    except Exception:
        m = re.search(r"\{.*\}", tail, flags=re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception as e:
                print(f"[router] json tail parse failed: {e}", file=sys.stderr)
        return {}


def route(user_msg: str, history: Optional[list[dict[str, str]]] = None) -> str:
    """Ask Gemini for a single-line routing decision."""
    messages = [{"role": "user", "content": user_msg}]
    if history:
        # Include last few turns for context
        trimmed = history[-6:]
        messages = trimmed + messages
    try:
        decision = chat_fast(messages, temperature=0.1, max_tokens=300, system=ROUTER_SYSTEM)
        line = (decision or "CHAT").strip().splitlines()[0].strip()
        return line or "CHAT"
    except Exception as e:
        print(f"[router] route error: {e}", file=sys.stderr)
        return "CHAT"


def answer(
    user_msg: str,
    history: Optional[list[dict[str, str]]] = None,
) -> Generator[str | dict[str, Any], None, None]:
    """Yield text chunks then a final {"__meta__": {...}} dict."""
    routing = route(user_msg, history)
    sources: list[dict[str, Any]] = []
    meta_routing = routing

    try:
        if routing.startswith("MEMORY"):
            hits = mem.search(user_msg, k=5)
            ctx = mem.format_for_prompt(hits)
            system = (
                "Answer using ONLY the memory context below when possible. "
                "Cite with [n] markers.\n\n" + ctx
            )
            for chunk in chat_grounded(
                user_msg, history=history, system=system, use_search=False
            ):
                if isinstance(chunk, dict) and "__meta__" in chunk:
                    sources = chunk["__meta__"].get("sources") or []
                else:
                    yield chunk

        elif routing.startswith("PROSPECT_SEARCH:"):
            q = _parse_json_tail(routing, "PROSPECT_SEARCH:")
            if not q:
                q = {"keywords": user_msg}
            results = search_all(q, providers=("apollo", "rocketreach", "zoominfo"))
            ctx_lines = []
            for i, p in enumerate(results[:20], 1):
                if p.get("error"):
                    ctx_lines.append(f"{i}. ERROR [{p.get('source')}]: {p.get('error')}")
                else:
                    ctx_lines.append(f"{i}. {prospect_to_text(p)}")
            system = (
                "Summarize these prospect search results for the user. "
                "Highlight emails when present, note gaps, suggest next enrich steps.\n\n"
                + "\n\n".join(ctx_lines)
            )
            for chunk in chat_grounded(
                user_msg, history=history, system=system, use_search=False
            ):
                if isinstance(chunk, dict) and "__meta__" in chunk:
                    sources = chunk["__meta__"].get("sources") or []
                else:
                    yield chunk
            sources.append({"title": "prospect_search", "url": "", "type": "prospects", "count": len(results)})

        elif routing.startswith("PROSPECT_ENRICH:"):
            ident = _parse_json_tail(routing, "PROSPECT_ENRICH:")
            result = enrich_fallthrough(ident or {"name": user_msg})
            system = (
                "Present this enriched prospect clearly as JSON-aware prose.\n\n"
                + json.dumps(result, default=str)[:6000]
            )
            for chunk in chat_grounded(
                user_msg, history=history, system=system, use_search=False
            ):
                if isinstance(chunk, dict) and "__meta__" in chunk:
                    sources = chunk["__meta__"].get("sources") or []
                else:
                    yield chunk

        elif routing.startswith("GMAIL_EXTRACT:"):
            gmail_q = routing[len("GMAIL_EXTRACT:") :].strip() or "newer_than:7d"
            batch = extract_batch(gmail_q, max_results=8)
            system = (
                "Summarize extracted inbox intelligence for the user.\n\n"
                + json.dumps(batch, default=str)[:8000]
            )
            for chunk in chat_grounded(
                user_msg, history=history, system=system, use_search=False
            ):
                if isinstance(chunk, dict) and "__meta__" in chunk:
                    sources = chunk["__meta__"].get("sources") or []
                else:
                    yield chunk

        elif routing.startswith("SCHEDULE_EMAIL:"):
            job = _parse_json_tail(routing, "SCHEDULE_EMAIL:")
            if not job.get("recipient_email"):
                yield "I couldn't schedule that — missing recipient_email in the routing JSON."
            else:
                from datetime import datetime, timedelta

                send_at = job.get("send_at") or (
                    datetime.now() + timedelta(days=1)
                ).replace(hour=9, minute=30, second=0).isoformat()
                result = schedule_email(
                    recipient_email=job["recipient_email"],
                    subject=job.get("subject") or "(no subject)",
                    html_body=job.get("html_body") or job.get("body") or "<p></p>",
                    send_at=send_at,
                    recipient_name=job.get("recipient_name"),
                    campaign=job.get("campaign"),
                    source=job.get("source"),
                )
                yield f"Scheduled email to {job['recipient_email']} at {send_at}.\n"
                yield f"Result: {json.dumps(result, default=str)}"

        else:
            # CHAT (default)
            for chunk in chat_grounded(
                user_msg, history=history, use_search=True
            ):
                if isinstance(chunk, dict) and "__meta__" in chunk:
                    sources = chunk["__meta__"].get("sources") or []
                else:
                    yield chunk

    except Exception as e:
        print(f"[router] answer error: {e}", file=sys.stderr)
        yield f"[error] {e}"

    yield {"__meta__": {"routing": meta_routing, "sources": sources}}
