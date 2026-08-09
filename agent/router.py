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
from gmail_client.attachments import document_context_from_attachments
from gmail_client.extract import extract_batch
from gmail_client.send import create_draft, send_email
from scheduling.client import schedule_email

ROUTER_SYSTEM = """You are a routing classifier for Relay, a prospect research and outreach tool.
Given the user message, output EXACTLY ONE line choosing one of:

CHAT
MEMORY
PROSPECT_SEARCH:<json>
PROSPECT_ENRICH:<json>
GMAIL_EXTRACT:<gmail query>
DRAFT_EMAIL:<compact-json>
SEND_EMAIL:<compact-json>
SCHEDULE_EMAIL:<compact-json>

Rules:
- CHAT: general questions, research, writing help — Gemini will use Google Search.
- MEMORY: user asks about saved notes/prospects already in memory.
- PROSPECT_SEARCH: find people. JSON keys may include titles, company_names, company_domains, locations, seniorities, keywords (comma strings or arrays).
- PROSPECT_ENRICH: enrich one person. JSON keys: first_name, last_name, email, company, linkedin_url, title.
- GMAIL_EXTRACT: pull structured data from inbox; after the colon put a Gmail search query.
- DRAFT_EMAIL: user asks to create/save one OR MANY Gmail drafts (not send, not schedule).
  Compact JSON ONLY — never include html_body.
  Single: {"recipient_email":"a@b.com","subject":"Hello"}
  Multi: {"batch":true,"recipient_emails":["a@b.com","b@c.com"],"subject":"Hello"}
  Or multi from last prospect search: {"batch":true,"from_prospects":true,"subject":"Hello"}
- SEND_EMAIL: user asks to send email(s) now (immediately). Same compact JSON shapes as DRAFT_EMAIL.
  Prefer SEND_EMAIL when they say send/email now/fire off (not draft, not schedule later).
- SCHEDULE_EMAIL: user wants to schedule/queue an email for later send (not a draft).
  Compact JSON ONLY — include recipient_email, subject, send_at (ISO if known).
  Optional: recipient_name, campaign.
  NEVER include html_body or the full email text in this line (it will be truncated).
  Example: SCHEDULE_EMAIL:{"recipient_email":"a@b.com","subject":"Hello","send_at":"2026-08-10T09:30:00"}
- Prefer DRAFT_EMAIL when the user says draft/compose/save draft.
- Prefer SEND_EMAIL when they say send now / send this email.
- Prefer SCHEDULE_EMAIL when they say schedule/send later/queue.
- If the user lists multiple emails or says "all prospects" / "everyone" for drafts/sends, use batch:true.
- Chat may include file attachments (PDF/text); do not mention them in the routing line.
  If the user asks to draft/send/schedule using an uploaded document, still choose
  DRAFT_EMAIL / SEND_EMAIL / SCHEDULE_EMAIL (document text is applied separately).

Output NOTHING except the single routing line.
"""

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_TEMPLATE_KEYS = ("first_name", "name", "title", "company", "email", "recipient_email")


def _apply_template(text: str, prospect: dict[str, Any]) -> str:
    """Substitute {first_name}, {name}, {title}, {company}, {email}."""
    if not text:
        return text or ""
    name = str(prospect.get("name") or prospect.get("recipient_name") or "").strip()
    name_parts = name.split(None, 1)
    first = str(prospect.get("first_name") or "").strip() or (
        name_parts[0] if name_parts else ""
    )
    mapping = {
        "first_name": first,
        "name": name,
        "title": str(prospect.get("title") or ""),
        "company": str(prospect.get("company") or ""),
        "email": str(
            prospect.get("email") or prospect.get("recipient_email") or ""
        ),
        "recipient_email": str(
            prospect.get("email") or prospect.get("recipient_email") or ""
        ),
    }
    out = text
    for key in _TEMPLATE_KEYS:
        out = out.replace("{" + key + "}", mapping.get(key) or "")
    return out


def _prospects_with_email(prospects: Optional[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for p in prospects or []:
        if p.get("error"):
            continue
        email = (p.get("email") or "").strip()
        if email:
            rows.append(p)
    return rows


def _parse_json_tail(routing: str, prefix: str) -> dict[str, Any]:
    """Parse JSON after a routing prefix; recover from markdown fences / truncation."""
    tail = routing[len(prefix) :].strip()
    if tail.startswith("```"):
        tail = re.sub(r"^```(?:json)?\s*", "", tail)
        tail = re.sub(r"\s*```$", "", tail)
    try:
        data = json.loads(tail)
        return data if isinstance(data, dict) else {}
    except Exception:
        m = re.search(r"\{.*\}", tail, flags=re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
                return data if isinstance(data, dict) else {}
            except Exception as e:
                print(f"[router] json tail parse failed: {e}", file=sys.stderr)
        partial: dict[str, Any] = {}
        for key in (
            "recipient_email",
            "subject",
            "send_at",
            "recipient_name",
            "campaign",
            "source",
        ):
            km = re.search(rf'"{key}"\s*:\s*"([^"]*)"', tail)
            if km:
                partial[key] = km.group(1)
        if re.search(r'"batch"\s*:\s*true', tail, re.I):
            partial["batch"] = True
        if re.search(r'"from_prospects"\s*:\s*true', tail, re.I):
            partial["from_prospects"] = True
        emails = re.findall(
            r'"recipient_emails"\s*:\s*\[([^\]]*)\]',
            tail,
        )
        if emails:
            partial["recipient_emails"] = re.findall(r'"([^"]+@[^"]+)"', emails[0])
        return partial


def _extract_email_job(
    user_msg: str,
    history: Optional[list[dict[str, str]]] = None,
    seed: Optional[dict[str, Any]] = None,
    *,
    for_schedule: bool = False,
    document_context: str = "",
) -> dict[str, Any]:
    """Recover a single email payload from the user message (+ optional PDF/text context)."""
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
    doc_block = ""
    if document_context.strip():
        doc_block = f"""

Uploaded document text (use this as source material for the email):
{document_context.strip()}
"""
    prompt = f"""Extract an email job from the conversation.

Return JSON with keys:
recipient_email (string),
recipient_emails (array of strings — when multiple recipients),
recipient_name (string),
subject (string — may include {{first_name}} {{name}} {{title}} {{company}} templates),
html_body (string — HTML email body; may include the same {{placeholders}}),
use_prospects (boolean — true if user wants drafts for last searched prospects),
{send_at_line}campaign (string),
source (string)

Rules for html_body:
- If uploaded document text is provided, write a clear professional email that uses
  the important facts/offers/details from that document (do not dump the raw PDF).
- Keep HTML simple (<p>, <ul>/<li>, <strong>).
- If the user already drafted body text, prefer refining that with document facts.

Conversation:
{hist_txt}

Latest user message:
{user_msg}
{doc_block}
Seed fields already known (prefer newer message if conflict):
{json.dumps(seed)}
"""
    try:
        raw = extract_json(
            prompt,
            system=(
                "Extract email fields. Support multiple recipients. "
                "When document text is present, ground the email body in it. "
                "Return valid JSON only."
            ),
            max_tokens=4000,
        )
        data = json.loads(raw)
        if isinstance(data, dict):
            merged = {**seed, **{k: v for k, v in data.items() if v not in (None, "")}}
            return merged
    except Exception as e:
        print(f"[router] email extract failed: {e}", file=sys.stderr)

    if not seed.get("recipient_email") and not seed.get("recipient_emails"):
        found = _EMAIL_RE.findall(user_msg)
        if len(found) == 1:
            seed["recipient_email"] = found[0]
        elif len(found) > 1:
            seed["recipient_emails"] = found
    if not seed.get("html_body"):
        if document_context.strip():
            seed["html_body"] = (
                f"<p>{user_msg}</p>"
                f"<p><em>Based on uploaded document:</em></p>"
                f"<p>{document_context.strip()[:1500]}</p>"
            )
        else:
            seed["html_body"] = f"<p>{user_msg}</p>"
    if not seed.get("subject"):
        seed["subject"] = "(no subject)"
    return seed


def _build_draft_jobs(
    payload: dict[str, Any],
    user_msg: str,
    history: Optional[list[dict[str, str]]] = None,
    prospects: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """Expand a draft payload into one job per recipient."""
    subject_tmpl = payload.get("subject") or "(no subject)"
    body_tmpl = payload.get("html_body") or payload.get("body") or f"<p>{user_msg}</p>"
    campaign = payload.get("campaign") or ""
    source = payload.get("source") or "chat_draft"

    use_prospects = bool(
        payload.get("from_prospects")
        or payload.get("use_prospects")
        or (
            payload.get("batch")
            and re.search(
                r"\b(all prospects|these prospects|last search|everyone we found)\b",
                user_msg,
                re.I,
            )
        )
    )
    # Explicit from_prospects / use_prospects always wins
    if payload.get("from_prospects") or payload.get("use_prospects"):
        use_prospects = True

    jobs: list[dict[str, Any]] = []

    if use_prospects:
        rows = _prospects_with_email(prospects)
        for p in rows:
            job = {
                "recipient_email": p.get("email"),
                "recipient_name": p.get("name") or "",
                "subject": _apply_template(subject_tmpl, p),
                "html_body": _apply_template(body_tmpl, p),
                "campaign": campaign,
                "source": source,
            }
            if payload.get("attachments"):
                job["attachments"] = payload["attachments"]
            jobs.append(job)
        if jobs:
            return jobs

    emails: list[str] = []
    if isinstance(payload.get("recipient_emails"), list):
        emails.extend(
            str(e).strip() for e in payload["recipient_emails"] if str(e).strip()
        )
    if payload.get("recipient_email"):
        emails.append(str(payload["recipient_email"]).strip())

    # Also scrape all emails from the user message when batch-ish
    scraped = _EMAIL_RE.findall(user_msg)
    if len(scraped) > 1:
        for e in scraped:
            if e not in emails:
                emails.append(e)
    elif not emails and scraped:
        emails.extend(scraped)

    # Deduplicate preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for e in emails:
        key = e.lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(e)

    # Match names from prospects when available
    by_email = {
        (p.get("email") or "").lower(): p for p in _prospects_with_email(prospects)
    }
    for email in uniq:
        p = by_email.get(email.lower()) or {
            "email": email,
            "name": payload.get("recipient_name") or "",
            "first_name": "",
            "title": "",
            "company": "",
        }
        if "email" not in p:
            p = {**p, "email": email}
        job = {
            "recipient_email": email,
            "recipient_name": p.get("name") or payload.get("recipient_name") or "",
            "subject": _apply_template(subject_tmpl, p),
            "html_body": _apply_template(body_tmpl, p),
            "campaign": campaign,
            "source": source,
        }
        if payload.get("attachments"):
            job["attachments"] = payload["attachments"]
        jobs.append(job)
    return jobs


def _gmail_attachment_payload(
    attachments: Optional[list[dict[str, Any]]],
) -> Optional[list[dict[str, Any]]]:
    """Normalize to Gmail API shape: [{name, data: bytes}]."""
    if not attachments:
        return None
    out: list[dict[str, Any]] = []
    for att in attachments:
        data = att.get("data")
        if data is None and att.get("data_base64"):
            import base64

            data = base64.b64decode(att["data_base64"])
        if data is None:
            continue
        out.append({"name": att.get("name") or "file", "data": data})
    return out or None


def _attach_note(
    attachments: Optional[list[dict[str, Any]]],
    *,
    used_document_context: bool = False,
) -> str:
    if not attachments:
        return ""
    names = ", ".join(a.get("name") or "file" for a in attachments)
    note = f" Attachments: {names}."
    if used_document_context:
        note += " (PDF/text used as email context.)"
    return note


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
        if not text:
            return "CHAT"
        # Keep full text for SCHEDULE_EMAIL / JSON routes (may be multi-line)
        upper = text.upper()
        for prefix in (
            "DRAFT_EMAIL:",
            "SEND_EMAIL:",
            "SCHEDULE_EMAIL:",
            "PROSPECT_SEARCH:",
            "PROSPECT_ENRICH:",
            "GMAIL_EXTRACT:",
        ):
            if upper.startswith(prefix) or text.startswith(prefix):
                # Normalize to the canonical prefix casing from the first line start
                lines = text.splitlines()
                first = lines[0] if lines else text
                # If model wrapped JSON onto later lines, reassemble after first prefix
                if prefix.endswith("EMAIL:") or prefix.startswith("PROSPECT"):
                    rest = text[len(first) :]
                    return (first + rest).strip()
                return first.strip()
        lines = text.splitlines()
        line = (lines[0] if lines else text).strip()
        return line or "CHAT"
    except Exception as e:
        print(f"[router] route error: {e}", file=sys.stderr)
        return "CHAT"


def answer(
    user_msg: str,
    history: Optional[list[dict[str, str]]] = None,
    context: Optional[dict[str, Any]] = None,
) -> Generator[str | dict[str, Any], None, None]:
    """Yield text chunks then a final {"__meta__": {...}} dict.

    context may include:
      prospects: list of normalized prospects (e.g. st.session_state.last_prospects)
    """
    routing = route(user_msg, history)
    sources: list[dict[str, Any]] = []
    meta_routing = routing
    prospects = (context or {}).get("prospects") or []
    chat_attachments = (context or {}).get("attachments") or []
    gmail_atts = _gmail_attachment_payload(chat_attachments)
    doc_context = document_context_from_attachments(chat_attachments)
    used_docs = bool(doc_context.strip())

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

        elif routing.startswith("DRAFT_EMAIL") or routing.startswith("SEND_EMAIL"):
            is_send = routing.startswith("SEND_EMAIL")
            prefix = "SEND_EMAIL:" if is_send else "DRAFT_EMAIL:"
            seed = (
                _parse_json_tail(routing, prefix)
                if routing.startswith(prefix)
                else {}
            )
            payload = _extract_email_job(
                user_msg,
                history=history,
                seed=seed,
                for_schedule=False,
                document_context=doc_context,
            )
            for flag in ("batch", "from_prospects", "use_prospects", "recipient_emails"):
                if flag in seed and flag not in payload:
                    payload[flag] = seed[flag]
            if chat_attachments:
                payload["attachments"] = chat_attachments

            jobs = _build_draft_jobs(
                payload, user_msg, history=history, prospects=prospects
            )
            action = "send" if is_send else "draft"
            if not jobs:
                yield (
                    f"I couldn't {action} — no recipient emails found. "
                    "List addresses, or search prospects first then say "
                    f"'{action} emails to all prospects'."
                )
            elif len(jobs) == 1:
                job = jobs[0]
                atts = _gmail_attachment_payload(job.get("attachments")) or gmail_atts
                if is_send:
                    result = send_email(
                        to=str(job["recipient_email"]).strip(),
                        subject=job.get("subject") or "(no subject)",
                        html_body=job.get("html_body") or "<p></p>",
                        recipient_name=job.get("recipient_name"),
                        attachments=atts,
                        campaign=job.get("campaign"),
                        source=job.get("source") or "chat_send",
                    )
                else:
                    result = create_draft(
                        to=str(job["recipient_email"]).strip(),
                        subject=job.get("subject") or "(no subject)",
                        html_body=job.get("html_body") or "<p></p>",
                        recipient_name=job.get("recipient_name"),
                        attachments=atts,
                        campaign=job.get("campaign"),
                        source=job.get("source") or "chat_draft",
                        track=False,
                    )
                if result.get("error"):
                    yield f"{action.title()} failed: {result['error']}"
                else:
                    if is_send:
                        yield (
                            f"Sent email to {job['recipient_email']} "
                            f"(message_id={result.get('message_id')})."
                            f"{_attach_note(atts, used_document_context=used_docs)}"
                        )
                    else:
                        yield (
                            f"Created Gmail draft to {job['recipient_email']} "
                            f"(draft_id={result.get('draft_id')}). "
                            "Open Gmail → Drafts to review and send."
                            f"{_attach_note(atts, used_document_context=used_docs)}"
                        )
                    yield f"\nResult: {json.dumps(result, default=str)}"
            else:
                yield f"{'Sending' if is_send else 'Creating'} {len(jobs)} emails…\n"
                results = []
                for job in jobs:
                    atts = _gmail_attachment_payload(job.get("attachments")) or gmail_atts
                    if is_send:
                        results.append(
                            send_email(
                                to=str(job["recipient_email"]).strip(),
                                subject=job.get("subject") or "(no subject)",
                                html_body=job.get("html_body") or "<p></p>",
                                recipient_name=job.get("recipient_name"),
                                attachments=atts,
                                campaign=job.get("campaign"),
                                source=job.get("source") or "chat_send_batch",
                            )
                        )
                    else:
                        job = {**job, "attachments": atts}
                        results.append(
                            create_draft(
                                to=str(job["recipient_email"]).strip(),
                                subject=job.get("subject") or "(no subject)",
                                html_body=job.get("html_body") or "<p></p>",
                                recipient_name=job.get("recipient_name"),
                                attachments=atts,
                                campaign=job.get("campaign"),
                                source=job.get("source") or "chat_draft_batch",
                                track=False,
                            )
                        )
                ok = [r for r in results if not r.get("error")]
                fail = [r for r in results if r.get("error")]
                yield f"Done: **{len(ok)}** ok"
                if fail:
                    yield f", **{len(fail)}** failed"
                yield f".{_attach_note(gmail_atts, used_document_context=used_docs)}\n"
                for r in ok[:50]:
                    target = r.get("to") or r.get("recipient_email")
                    rid = r.get("draft_id") or r.get("message_id")
                    yield f"- {action} → {target} (id={rid})\n"
                for r in fail[:20]:
                    yield f"- failed → {r.get('to') or '?'}: {r.get('error')}\n"
                if not is_send:
                    yield "\nOpen Gmail → Drafts to review and send."

        elif routing.startswith("SCHEDULE_EMAIL"):
            # Accept truncated "SCHEDULE_EMAIL:{"recipient_email" lines.
            prefix = "SCHEDULE_EMAIL:"
            seed = (
                _parse_json_tail(routing, prefix)
                if routing.startswith(prefix)
                else {}
            )
            job = _extract_email_job(
                user_msg,
                history=history,
                seed=seed,
                for_schedule=True,
                document_context=doc_context,
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
                    attachments=chat_attachments or None,
                )
                yield f"Scheduled email to {job['recipient_email']} at {send_at}."
                yield f"{_attach_note(chat_attachments, used_document_context=used_docs)}\n"
                yield f"Result: {json.dumps(result, default=str)}"

        else:
            # CHAT (default) — include uploaded PDF/text when present
            system = None
            if used_docs:
                system = (
                    "The user uploaded document(s). Use this extracted text when "
                    "answering, summarizing, or helping draft email copy. Prefer "
                    "facts from the documents over speculation.\n\n"
                    f"{doc_context}"
                )
            for chunk in chat_grounded(
                user_msg, history=history, system=system, use_search=True
            ):
                if isinstance(chunk, dict) and "__meta__" in chunk:
                    sources = chunk["__meta__"].get("sources") or []
                else:
                    yield chunk

    except Exception as e:
        print(f"[router] answer error: {e}", file=sys.stderr)
        yield f"[error] {e}"

    yield {"__meta__": {"routing": meta_routing, "sources": sources}}
