# NOTE: Router uses chat_fast with a compact decision line. SCHEDULE_EMAIL bodies
# are extracted separately via extract_json so long HTML cannot truncate routing.
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta
from typing import Any, Generator, Optional

from connectors.ingest_to_memory import prospect_to_text
from connectors.prospects import enrich_fallthrough, search_all
from core import memory as mem
from core.llm import chat_fast, chat_grounded, extract_json
from gmail_client.extract import extract_batch
from gmail_client.send import create_draft
from scheduling.client import schedule_email

ROUTER_SYSTEM = """You are a routing classifier for Relay, a prospect research and outreach tool.
Given the user message, output EXACTLY ONE line choosing one of:

CHAT
MEMORY
PROSPECT_SEARCH:<json>
PROSPECT_ENRICH:<json>
GMAIL_EXTRACT:<gmail query>
DRAFT_EMAIL:<compact-json>
SCHEDULE_EMAIL:<compact-json>

Rules:
- CHAT: general questions, research, writing help — Gemini will use Google Search.
- MEMORY: user asks about saved notes/prospects already in memory.
- PROSPECT_SEARCH: find people. JSON keys may include titles, company_names, company_domains, locations, seniorities, keywords (comma strings or arrays).
- PROSPECT_ENRICH: enrich one person. JSON keys: first_name, last_name, email, company, linkedin_url, title.
- GMAIL_EXTRACT: pull structured data from inbox; after the colon put a Gmail search query.
- DRAFT_EMAIL: user asks to create/save a Gmail draft (not send, not schedule).
  Compact JSON ONLY — recipient_email, subject. Optional: recipient_name, campaign.
  NEVER include html_body in this line.
  Example: DRAFT_EMAIL:{"recipient_email":"a@b.com","subject":"Hello"}
- SCHEDULE_EMAIL: user wants to schedule/queue an email for later send (not a draft).
  Compact JSON ONLY — include recipient_email, subject, send_at (ISO if known).
  Optional: recipient_name, campaign.
  NEVER include html_body or the full email text in this line (it will be truncated).
  Example: SCHEDULE_EMAIL:{"recipient_email":"a@b.com","subject":"Hello","send_at":"2026-08-10T09:30:00"}
- Prefer DRAFT_EMAIL over SCHEDULE_EMAIL when the user says draft/compose/save draft.
- Prefer SCHEDULE_EMAIL when they say schedule/send later/queue.

Output NOTHING except the single routing line.
"""

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _parse_json_tail(routing: str, prefix: str) -> dict[str, Any]:
    """Parse JSON after a routing prefix; recover from markdown fences / truncation."""
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
        # Pull whatever scalar fields we can from a truncated object
        partial: dict[str, Any] = {}
        for key in (
            "recipient_email",
            "subject",
            "send_at",
            "recipient_name",
            "campaign",
            "source",
        ):
            km = re.search(
                rf'"{key}"\s*:\s*"([^"]*)"',
                tail,
            )
            if km:
                partial[key] = km.group(1)
        return partial


def _extract_email_job(
    user_msg: str,
    history: Optional[list[dict[str, str]]] = None,
    seed: Optional[dict[str, Any]] = None,
    *,
    for_schedule: bool = False,
) -> dict[str, Any]:
    """Recover full email payload from the user message (not the truncated route line)."""
    seed = dict(seed or {})
    hist_txt = ""
    if history:
        hist_txt = "\n".join(
            f"{m.get('role')}: {m.get('content')}" for m in history[-6:]
        )
    default_send = (datetime.now() + timedelta(days=1)).replace(
        hour=9, minute=30, second=0, microsecond=0
    ).isoformat()
    send_at_line = (
        f"send_at (ISO 8601 datetime; default {default_send} if unspecified),\n"
        if for_schedule
        else ""
    )
    prompt = f"""Extract an email job from the conversation.

Return JSON with keys:
recipient_email (string, required),
recipient_name (string),
subject (string),
html_body (string — convert plain text to simple HTML paragraphs if needed),
{send_at_line}campaign (string),
source (string)

Conversation:
{hist_txt}

Latest user message:
{user_msg}

Seed fields already known (prefer newer message if conflict):
{json.dumps(seed)}
"""
    try:
        raw = extract_json(
            prompt,
            system="Extract email fields. Return valid JSON only.",
            max_tokens=4000,
        )
        data = json.loads(raw)
        if isinstance(data, dict):
            merged = {**seed, **{k: v for k, v in data.items() if v not in (None, "")}}
            return merged
    except Exception as e:
        print(f"[router] email extract failed: {e}", file=sys.stderr)

    if not seed.get("recipient_email"):
        found = _EMAIL_RE.findall(user_msg)
        if found:
            seed["recipient_email"] = found[0]
    if not seed.get("html_body"):
        seed["html_body"] = f"<p>{user_msg}</p>"
    if not seed.get("subject"):
        seed["subject"] = "(no subject)"
    return seed


def _resolve_recipient(
    job: dict[str, Any],
    user_msg: str,
    history: Optional[list[dict[str, str]]] = None,
) -> dict[str, Any]:
    if job.get("recipient_email"):
        return job
    blob = user_msg + "\n" + "\n".join(
        (m.get("content") or "") for m in (history or [])
    )
    found = _EMAIL_RE.findall(blob)
    if found:
        job["recipient_email"] = found[-1]
    return job


def route(user_msg: str, history: Optional[list[dict[str, str]]] = None) -> str:
    """Ask Gemini for a single-line routing decision."""
    messages = [{"role": "user", "content": user_msg}]
    if history:
        trimmed = history[-6:]
        messages = trimmed + messages
    try:
        # Keep max_tokens modest — schedule bodies are extracted separately.
        decision = chat_fast(
            messages, temperature=0.1, max_tokens=500, system=ROUTER_SYSTEM
        )
        text = (decision or "CHAT").strip()
        # Keep full text for SCHEDULE_EMAIL / JSON routes (may be multi-line)
        upper = text.upper()
        for prefix in (
            "DRAFT_EMAIL:",
            "SCHEDULE_EMAIL:",
            "PROSPECT_SEARCH:",
            "PROSPECT_ENRICH:",
            "GMAIL_EXTRACT:",
        ):
            if upper.startswith(prefix) or text.startswith(prefix):
                # Normalize to the canonical prefix casing from the first line start
                first = text.splitlines()[0]
                # If model wrapped JSON onto later lines, reassemble after first prefix
                if prefix.endswith("EMAIL:") or prefix.startswith("PROSPECT"):
                    rest = text[len(first) :]
                    return (first + rest).strip()
                return first.strip()
        line = text.splitlines()[0].strip()
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
            sources.append(
                {
                    "title": "prospect_search",
                    "url": "",
                    "type": "prospects",
                    "count": len(results),
                }
            )

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

        elif routing.startswith("DRAFT_EMAIL"):
            prefix = "DRAFT_EMAIL:"
            seed = (
                _parse_json_tail(routing, prefix)
                if routing.startswith(prefix)
                else {}
            )
            job = _extract_email_job(
                user_msg, history=history, seed=seed, for_schedule=False
            )
            job = _resolve_recipient(job, user_msg, history)
            if not job.get("recipient_email"):
                yield (
                    "I couldn't create a draft — no recipient email found. "
                    "Please include an address like name@company.com."
                )
            else:
                html_body = (
                    job.get("html_body") or job.get("body") or f"<p>{user_msg}</p>"
                )
                result = create_draft(
                    to=str(job["recipient_email"]).strip(),
                    subject=job.get("subject") or "(no subject)",
                    html_body=html_body,
                    recipient_name=job.get("recipient_name"),
                    campaign=job.get("campaign"),
                    source=job.get("source") or "chat_draft",
                    track=False,
                )
                if result.get("error"):
                    yield f"Draft failed: {result['error']}"
                else:
                    yield (
                        f"Created Gmail draft to {job['recipient_email']} "
                        f"(draft_id={result.get('draft_id')}). "
                        "Open Gmail → Drafts to review and send."
                    )
                    yield f"\nResult: {json.dumps(result, default=str)}"

        elif routing.startswith("SCHEDULE_EMAIL"):
            # Accept truncated "SCHEDULE_EMAIL:{"recipient_email" lines.
            prefix = "SCHEDULE_EMAIL:"
            seed = (
                _parse_json_tail(routing, prefix)
                if routing.startswith(prefix)
                else {}
            )
            job = _extract_email_job(
                user_msg, history=history, seed=seed, for_schedule=True
            )
            job = _resolve_recipient(job, user_msg, history)

            if not job.get("recipient_email"):
                yield (
                    "I couldn't schedule that — no recipient email found. "
                    "Please include an address like name@company.com."
                )
            else:
                send_at = job.get("send_at") or (
                    datetime.now() + timedelta(days=1)
                ).replace(hour=9, minute=30, second=0, microsecond=0).isoformat()
                html_body = (
                    job.get("html_body") or job.get("body") or f"<p>{user_msg}</p>"
                )
                result = schedule_email(
                    recipient_email=str(job["recipient_email"]).strip(),
                    subject=job.get("subject") or "(no subject)",
                    html_body=html_body,
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
