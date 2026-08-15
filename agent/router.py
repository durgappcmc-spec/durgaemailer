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
from connectors.zoominfo import (
    extract_linkedin_url,
    extract_linkedin_urls,
    names_from_linkedin_url,
)
from core.auto_sync import auto_ingest_prospects, ingest_mailbox_messages
from core.enrich_cache import (
    email_not_found_prompt,
    format_enrichment_panel,
    get_cached_enrichment,
    put_cached_enrichment,
)
from core.style_draft import (
    compose_styled_email,
    directive_to_list,
    fetch_latest_sent_to,
    looks_like_bulk_request,
    parse_directives,
)
from core import memory as mem
from core.llm import chat_fast, chat_grounded, extract_json, grounded_collect
from agent.intent import (
    IntentPlan,
    classify_email_roles,
    filter_recipient_emails,
    org_label_from_email,
    parse_contact_search_company,
    parse_explicit_draft_company,
    parse_gmail_message_id,
    parse_like_sent_request,
    parse_mailbox_list_index,
    parse_named_person_contact,
    plan_request,
    plan_summary,
    resolve_like_sent_from_history,
    resolve_to_emails_from_history,
    wants_contact_search,
    wants_linkedin_contact_lookup,
    wants_live_zoominfo_search,
    wants_previous_chat_recipient,
    wants_prospect_list_recipients,
    wants_search_then_draft,
)
from agent.research_pipeline import (
    discover_orgs_from_web,
    iter_enrich_orgs_on_zoominfo,
    run_research_then_zoom,
    wants_research_then_zoom,
)
from agent.session_context import (
    chat_grounding_system,
    prefers_chat_over_search,
)
from agent.run_control import is_cancelled, stopped_message
from agent.limits import (
    DEFAULT_SEARCH_LIMIT,
    MAX_EMAILS,
    apply_email_cap,
    parse_research_limits,
)
from gmail_client.attachments import document_context_from_attachments
from gmail_client.extract import (
    contacts_from_mailbox,
    extract_batch,
    extract_inbox_and_sent,
    filter_messages,
    find_sent_to_company,
    get_message,
    pick_best_sent_reference,
)
from gmail_client.send import (
    create_draft,
    default_cc_emails,
    default_from_email,
    send_email,
)
from scheduling.client import schedule_email

ROUTER_SYSTEM = """You are a routing classifier for Relay, a CSR outreach tool (Karuna Media).
Given the user message, output EXACTLY ONE line choosing one of:

CHAT
MEMORY
PROSPECT_SEARCH:<json>
PROSPECT_ENRICH:<json>
RESEARCH_THEN_ZOOM:<json>
GMAIL_EXTRACT:<gmail query>
DRAFT_EMAIL:<compact-json>
SEND_EMAIL:<compact-json>
SCHEDULE_EMAIL:<compact-json>

Rules:
- CHAT: general questions, research, writing help — Gemini will use Google Search.
- MEMORY: user asks about saved notes/prospects already in memory.
- RESEARCH_THEN_ZOOM: ONLY when the user asks to FIND/DISCOVER orgs matching a MISSION
  (e.g. "find NGOs for girls 16+ skilling in Noida") then ZoomInfo contacts and/or drafts.
  Do NOT choose this just because they say CSR, csr@karunamedia.org, Karuna, partnership,
  or "from CSR" — that means the SENDER identity for drafts, not org discovery.
  Example: RESEARCH_THEN_ZOOM:{"org_limit":25,"contacts_per_org":5,"draft":true}
  Set draft:true if they also ask to draft/write personalized emails in the same message.
  Set send:true only if they explicitly say send now.
  Honor user volumes up to 100 (e.g. "40 NGOs", "100 emails", "as many as needed").
- PROSPECT_SEARCH: find people when the user already knows company/title filters.
  JSON keys may include titles, company_names, company_domains, locations, seniorities, keywords, providers (array), limit.
  Prefer ZoomInfo when the user says ZoomInfo / ZI. Example:
  PROSPECT_SEARCH:{"titles":["CEO"],"company_names":["Acme"],"providers":["zoominfo"],"limit":50}
  "search for contact from RateGain Travel Technologies" →
  PROSPECT_SEARCH:{"company_names":["RateGain Travel Technologies"],"providers":["zoominfo"],"limit":25}
  Same-turn: "search contacts from Sterlite Tech and create draft like email@…" →
  PROSPECT_SEARCH with company_names + draft:true + like_sent_to (ZoomInfo first, then draft).
  Do NOT choose DRAFT_EMAIL for search-for-contact requests.
  Default limit 50; allow up to 100 when the user asks for a large list.
  Never ask the user for ZoomInfo credentials.
  After a prospect / research search, if the user asks to email/draft/send to that list
  (e.g. "draft to all", "these prospects", "to above") AND did not name a specific
  address via "draft to <email>", use DRAFT_EMAIL or SEND_EMAIL with
  {"batch":true,"from_prospects":true,"subject":"..."}.
  Recipient rule: If the user's current message names one or more specific email
  addresses via 'draft to', 'send to', 'email <addr>', or 'to <addr>', those
  addresses are the ONLY recipients. Do not add any recipient from prior
  conversation turns, enrichment history, prospect lists, or memory. Do not fan
  out. Never draft more than one email per explicitly named address. If no
  address is explicitly named in the current message and multiple prospects exist
  in context, ask the user who to draft to instead of guessing.
- PROSPECT_ENRICH: enrich people. JSON keys: first_name, last_name, email, company,
  linkedin_url, linkedin_urls (array). When the user pastes LinkedIn profile URL(s)
  or says "get contacts for these linkedin profiles", ALWAYS use PROSPECT_ENRICH
  with linkedin_urls set to EVERY /in/ URL in the message (not only the first).
  Look each up on ZoomInfo one by one. If they also ask to draft/send, still
  choose PROSPECT_ENRICH first (draft after contacts are saved).
- GMAIL_EXTRACT: read / list / filter Gmail. After the colon put a Gmail search query (NOT prose).
- DRAFT_EMAIL: create Gmail drafts for review (default for outreach). Compact JSON ONLY — never html_body.
  Single: {"recipient_email":"a@b.com","subject":"Hello","cc":["x@y.com","z@y.com"]}
  Multi: {"batch":true,"recipient_emails":["a@b.com"],"cc":["x@y.com","z@y.com"]}
  From last prospects: {"batch":true,"from_prospects":true,"subject":"Hello"}
  From mailbox: {"batch":true,"from_mailbox":true,"subject":"Re: {prior_subject}"}
  Like a prior sent email: {"like_sent_to":"IndiaMART","like_sent_for":"Acme"}
  From a Sent message id: {"like_sent_message_id":"18abc...","like_sent_for":"Acme"}
  Include ALL cc addresses the user listed. Put ignored emails in "ignore_emails".
  Never put csr@karunamedia.org or CC addresses in recipient_email(s).
  Known CC aliases (use these when only a name is given):
  Deepti → deepti.87.srivastava@gmail.com; Raahul/Rahul → raahul.ppcm@gmail.com.
  When user says "create email like sent to X" / "like info@org.org in sent items",
  use DRAFT_EMAIL with like_sent_to set (and like_sent_for for the new company if named).
  like_sent_to may be a company name OR a Sent recipient email — never put that
  reference email in recipient_email(s).
  When user gives a Gmail message id / "email id" / "#N from sent", use DRAFT_EMAIL with
  like_sent_message_id (and like_sent_for if they name a new company).
  Use recent chat history when they say "like that" / "same as before" without repeating X.
  Do NOT use RESEARCH_THEN_ZOOM for style-clone requests.
- SEND_EMAIL: send now only. Same compact JSON shapes as DRAFT_EMAIL.
- SCHEDULE_EMAIL: schedule later. Compact JSON with recipient_email, subject, send_at.
  NEVER include html_body in routing lines.
- Prefer DRAFT_EMAIL for draft/compose / CSR outreach from csr@.
- Prefer SEND_EMAIL only for send now / fire off.
- Prefer SCHEDULE_EMAIL for schedule/send later/queue.
- If user says ignore/skip/don't use an email, never route it as a recipient.
- Chat may include file attachments; do not mention them in the routing line.
- When prefixed with [ATTACHED FILES: ...], files are already uploaded — do not ask to attach.
- Use recent chat history for follow-ups (previous recipient, company, like-sent template).
  Do not invent emails or orgs that are not in the conversation.

Output NOTHING except the single routing line.
"""

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_TEMPLATE_KEYS = (
    "first_name",
    "name",
    "title",
    "designation",
    "name_with_title",
    "company",
    "email",
    "recipient_email",
    "prior_subject",
    "prior_summary",
    "org_focus",
    "org_website",
    "why_match",
    "phone",
    "mobile",
    "linkedin_url",
)
# User wants a file used as general chat/research context
_CONTEXT_FILE_RE = re.compile(
    r"\b(based on|using|from|read|summarize|analyse|analyze|review|look at)\b.*\b("
    r"file|pdf|doc|document|deck|brochure|proposal|spreadsheet|excel|pptx?|image|upload"
    r")\b|\b(this|the|my)\s+(file|pdf|doc|document|deck|upload)\b",
    re.I,
)
# User wants a real email attachment on draft/send/schedule
_EMAIL_ATTACH_RE = re.compile(
    r"\b("
    r"attach(\s|$)|attachment|attachments|"
    r"with\s+(the\s+)?(file|pdf|doc|document|deck)|"
    r"include\s+(the\s+)?(file|pdf|attachment)|"
    r"attach(ed|ing)\b"
    r")\b",
    re.I,
)


def _wants_email_attachment(user_msg: str) -> bool:
    return bool(_EMAIL_ATTACH_RE.search(user_msg or ""))


def _prefer_draft_over_send(user_msg: str, want_send: bool) -> bool:
    """Prefer creating a new draft for review unless the user clearly says send now.

    Even when routing says SEND_EMAIL / do_send, we draft by default so the
    user can review From/signature/CC before anything goes out.
    """
    if not want_send:
        return True
    msg = user_msg or ""
    # Explicit force-send phrases
    if re.search(
        r"\b("
        r"send\s+now|"
        r"actually\s+send|"
        r"fire\s+off|"
        r"email\s+them\s+now|"
        r"don'?t\s+draft|"
        r"do\s+not\s+draft|"
        r"skip\s+(the\s+)?draft|"
        r"send\s+immediately"
        r")\b",
        msg,
        re.I,
    ):
        return False
    # Soft "send" without "now" → still draft for review
    return True


def _wants_file_context(user_msg: str) -> bool:
    return bool(_CONTEXT_FILE_RE.search(user_msg or "") or _EMAIL_ATTACH_RE.search(user_msg or ""))


def _ask_for_upload(*, for_email_attach: bool = False) -> str:
    if for_email_attach:
        return (
            "Please **upload the file** with the **paperclip** on the chat box "
            "to attach it, then send the same draft/send/schedule request again."
        )
    return (
        "Please **upload the file** with the **paperclip** on the chat box "
        "for context, then ask your question again."
    )



def _attachment_names(attachments: Optional[list[dict[str, Any]]]) -> list[str]:
    return [str(a.get("name") or "file") for a in (attachments or [])]


def _route_user_msg(
    user_msg: str,
    attachments: Optional[list[dict[str, Any]]] = None,
) -> str:
    """Annotate the message for the router when files are already staged."""
    names = _attachment_names(attachments)
    if not names:
        return user_msg
    return f"[ATTACHED FILES ALREADY UPLOADED: {', '.join(names)}]\n{user_msg}"


def _apply_template(text: str, prospect: dict[str, Any]) -> str:
    """Substitute {first_name}, {name}, {title}/{designation}, {company}, {email}."""
    if not text:
        return text or ""
    name = str(prospect.get("name") or prospect.get("recipient_name") or "").strip()
    name_parts = name.split(None, 1)
    first = str(prospect.get("first_name") or "").strip() or (
        name_parts[0] if name_parts else ""
    )
    title = (
        str(prospect.get("title") or "").strip()
        or str(prospect.get("designation") or "").strip()
        or str(prospect.get("recipient_title") or "").strip()
    )
    # Greet by first name only — never "Sushmita (ESG Associate)"
    name_with_title = first or name or title
    mapping = {
        "first_name": first,
        "name": name,
        "title": title,
        "designation": title,
        "name_with_title": name_with_title,
        "company": str(prospect.get("company") or ""),
        "email": str(
            prospect.get("email") or prospect.get("recipient_email") or ""
        ),
        "recipient_email": str(
            prospect.get("email") or prospect.get("recipient_email") or ""
        ),
        "prior_subject": str(prospect.get("prior_subject") or ""),
        "prior_summary": str(prospect.get("prior_summary") or ""),
        "org_focus": str(prospect.get("org_focus") or ""),
        "org_website": str(prospect.get("org_website") or ""),
        "why_match": str(prospect.get("why_match") or ""),
        "phone": str(prospect.get("phone") or ""),
        "mobile": str(prospect.get("mobile") or prospect.get("phone") or ""),
        "linkedin_url": str(prospect.get("linkedin_url") or ""),
    }
    out = text
    for key in _TEMPLATE_KEYS:
        out = out.replace("{" + key + "}", mapping.get(key) or "")
        out = out.replace("{{" + key + "}}", mapping.get(key) or "")
    return out


def _ensure_designation_in_greeting(
    html: str,
    *,
    first_name: str = "",
    title: str = "",
) -> str:
    """Rewrite the opening greeting to this recipient's first name only."""
    from gmail_client.html_format import ensure_designation_in_greeting

    return ensure_designation_in_greeting(
        html, first_name=first_name, title=title
    )


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
        parts: list[str] = []
        for m in history[-12:]:
            role = m.get("role") or "?"
            content = (m.get("content") or "").strip()
            if len(content) > 2500:
                content = content[:2500] + "…"
            parts.append(f"{role}: {content}")
        hist_txt = "\n".join(parts)
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
recipient_email (string — To only; never put CC or from/CSR here),
recipient_emails (array — To only when multiple),
cc (array of ALL CC addresses the user listed — never drop any),
ignore_emails (array — addresses user said to ignore/skip/don't use),
from_email (string — usually csr@karunamedia.org),
recipient_name (string),
subject (string — may include {{first_name}} {{name}} {{title}} {{company}} templates),
html_body (string — HTML email body; may include the same {{placeholders}}),
use_prospects (boolean — true if user wants drafts for last searched prospects),
{send_at_line}campaign (string),
source (string)

Rules for recipients:
- csr@karunamedia.org is From, never To.
- Addresses after "cc" go ONLY in cc (include every one).
- Addresses after ignore/skip/don't use go ONLY in ignore_emails.
- Do not invent NGO lists; extract the email job only.
- If the latest message is a short follow-up ("draft to them", "same for Flipkart",
  "as per chat", "previous email"), pull To/CC/subject/body intent from earlier
  messages in the conversation. NEVER invent a recipient email.
- A Sent-template address ("like info@… in sent") is NOT the new To unless the
  user explicitly asks to email that same address.

Rules for html_body:
- If uploaded document text is provided, write a clear professional email that uses
  the important facts/offers/details from that document (do not dump the raw PDF).
- Keep HTML simple (<p>, <ul>/<li>, <strong>).
- Write the email body as normal prose. Do NOT insert manual line breaks inside paragraphs. Separate paragraphs with exactly one blank line. Do not indent. Use single spaces between words and single spaces after punctuation. No trailing spaces.
- Do not use markdown (no **bold**, no bullet dashes) unless a style template in the conversation uses them.
- If the user already drafted body text in prior chat turns, prefer refining that.
- Do not invent program details, amounts, or links that were not in the conversation
  or uploaded documents.
- For bulk follow-ups from inbox/sent, use placeholders
  {{first_name}}, {{name}}, {{company}}, {{prior_subject}}, {{prior_summary}}
  so each recipient gets a personalized message.

Conversation (source of truth — do not contradict or invent beyond it):
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
                "Extract email fields from conversation only. "
                "Never invent recipient emails or body facts not present in chat/docs. "
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
        roles = classify_email_roles(user_msg)
        if roles.to:
            if len(roles.to) == 1:
                seed["recipient_email"] = roles.to[0]
            else:
                seed["recipient_emails"] = roles.to
        if roles.cc and not seed.get("cc"):
            seed["cc"] = roles.cc
        if roles.ignore and not seed.get("ignore_emails"):
            seed["ignore_emails"] = roles.ignore
        if roles.from_email and not seed.get("from_email"):
            seed["from_email"] = roles.from_email
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
    mailbox_messages: Optional[list[dict[str, Any]]] = None,
    *,
    plan: Optional[IntentPlan] = None,
) -> list[dict[str, Any]]:
    """Expand a draft payload into one job per recipient."""
    subject_tmpl = payload.get("subject") or "(no subject)"
    body_tmpl = payload.get("html_body") or payload.get("body") or f"<p>{user_msg}</p>"
    campaign = payload.get("campaign") or ""
    source = payload.get("source") or "chat_draft"
    plan = plan or IntentPlan(
        from_email=payload.get("from_email") or default_from_email(),
        cc=list(payload.get("cc") or []),
        ignore_emails=list(payload.get("ignore_emails") or []),
    )
    block = plan.non_recipient_emails()

    jobs: list[dict[str, Any]] = []

    # Mailbox / prospect batch paths
    from_mailbox = bool(
        payload.get("from_mailbox")
        or payload.get("use_mailbox")
        or payload.get("follow_up")
        or re.search(
            r"\b(follow[- ]?ups?|from (my )?(inbox|sent|mailbox)|last (mail|inbox|sent|extract)|everyone (i|we) (emailed|contacted))\b",
            user_msg or "",
            re.I,
        )
    )
    from_prospects = bool(
        payload.get("from_prospects")
        or payload.get("use_prospects")
        or wants_prospect_list_recipients(user_msg or "")
        or (
            payload.get("batch")
            and re.search(
                r"\b(all prospects|these prospects|last search|everyone we found)\b",
                user_msg or "",
                re.I,
            )
        )
    )
    dirs = parse_directives(user_msg or "")
    if dirs.get("explicit_recipient_lock"):
        from_mailbox = False
        from_prospects = False
        locked = [
            a
            for a in directive_to_list(dirs)
            if a.lower() not in {e.lower() for e in (dirs.get("ignore") or [])}
        ]
        payload = dict(payload)
        payload.pop("from_prospects", None)
        payload.pop("use_prospects", None)
        payload.pop("from_mailbox", None)
        if len(locked) == 1:
            payload["recipient_email"] = locked[0]
            payload.pop("recipient_emails", None)
            payload["batch"] = False
        elif locked:
            payload["recipient_emails"] = locked
            payload.pop("recipient_email", None)
            payload["batch"] = True

    if from_mailbox and mailbox_messages:
        msgs = list(mailbox_messages or [])
        filt = str(payload.get("mailbox_filter") or payload.get("filter") or "").strip()
        if not filt:
            m = re.search(
                r"\b(?:about|regarding|filter(?:ed)?(?:\s+by)?|matching|with subject)\s+(.+)$",
                user_msg,
                re.I,
            )
            if m:
                filt = m.group(1).strip(" .")
        if filt:
            msgs = filter_messages(msgs, filt)
        prefer = "auto"
        if re.search(r"\binbox\b", user_msg, re.I) and not re.search(
            r"\bsent\b", user_msg, re.I
        ):
            prefer = "inbox"
        elif re.search(r"\bsent\b", user_msg, re.I) and not re.search(
            r"\binbox\b", user_msg, re.I
        ):
            prefer = "sent"
        contacts = contacts_from_mailbox(msgs, prefer=prefer)
        for p in contacts:
            email = (p.get("email") or "").strip()
            if not email or email.lower() in block:
                continue
            p = {**p, "email": email, "recipient_email": email}
            job = {
                "recipient_email": email,
                "recipient_name": p.get("name") or "",
                "title": p.get("title") or p.get("designation") or "",
                "company": p.get("company") or "",
                "subject": _apply_template(subject_tmpl, p),
                "html_body": _apply_template(body_tmpl, p),
                "campaign": campaign,
                "source": source or "chat_mailbox_followup",
            }
            if payload.get("attachments"):
                job["attachments"] = payload["attachments"]
            if payload.get("from_email"):
                job["from_email"] = payload["from_email"]
            if payload.get("cc") is not None:
                job["cc"] = payload["cc"]
            jobs.append(job)
        if jobs:
            return jobs

    if from_prospects:
        for p in _prospects_with_email(prospects):
            email = (p.get("email") or "").strip()
            if not email or email.lower() in block:
                continue
            p = {**p, "email": email, "recipient_email": email}
            job = {
                "recipient_email": email,
                "recipient_name": p.get("name") or "",
                "title": p.get("title") or p.get("designation") or "",
                "company": p.get("company") or "",
                "subject": _apply_template(subject_tmpl, p),
                "html_body": _apply_template(body_tmpl, p),
                "campaign": campaign,
                "source": source or "prospect_batch",
            }
            if payload.get("attachments"):
                job["attachments"] = payload["attachments"]
            if payload.get("from_email"):
                job["from_email"] = payload["from_email"]
            if payload.get("cc") is not None:
                job["cc"] = payload["cc"]
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
    if plan.to_emails and not dirs.get("explicit_recipient_lock"):
        emails.extend(plan.to_emails)

    # Never treat From / CC / ignored addresses as To (old bug: scraped all emails).
    emails = filter_recipient_emails(emails, plan=plan)

    # Only use explicit To roles if we still have no recipients
    if not emails:
        roles = classify_email_roles(user_msg)
        emails = filter_recipient_emails(roles.to, plan=plan)

    seen: set[str] = set()
    uniq: list[str] = []
    for e in emails:
        key = e.lower()
        if key in seen or key in block:
            continue
        seen.add(key)
        uniq.append(e)

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
            "prior_subject": "",
            "prior_summary": "",
        }
        p = {**p, "email": email, "recipient_email": email}
        job = {
            "recipient_email": email,
            "recipient_name": p.get("name") or payload.get("recipient_name") or "",
            "title": p.get("title")
            or p.get("designation")
            or payload.get("title")
            or "",
            "company": p.get("company") or payload.get("company") or "",
            "subject": _apply_template(subject_tmpl, p),
            "html_body": _apply_template(body_tmpl, p),
            "campaign": campaign,
            "source": source,
        }
        if payload.get("attachments"):
            job["attachments"] = payload["attachments"]
        if payload.get("from_email"):
            job["from_email"] = payload["from_email"]
        if payload.get("cc") is not None:
            job["cc"] = payload["cc"]
        jobs.append(job)
    return jobs


def _normalize_gmail_query(user_msg: str, raw_q: str) -> tuple[str, str]:
    """Return (gmail_query, mailbox_tag) from router tail + user wording."""
    q = (raw_q or "").strip().strip("`").strip()
    # Strip accidental prose prefixes
    q = re.sub(r"^(query|gmail|search)\s*[:=]\s*", "", q, flags=re.I).strip()
    msg = (user_msg or "").lower()

    days = 14
    m = re.search(r"\b(last|past)\s+(\d+)\s*days?\b", msg)
    if m:
        days = max(1, int(m.group(2)))
    elif re.search(r"\btoday\b", msg):
        days = 1
    elif re.search(r"\bthis week\b", msg):
        days = 7
    elif re.search(r"\bthis month\b", msg):
        days = 30

    want_sent = bool(re.search(r"\bsent\b", msg)) and not re.search(
        r"\binbox\b", msg
    )
    want_inbox = bool(re.search(r"\binbox\b", msg)) and not re.search(
        r"\bsent\b", msg
    )
    want_both = bool(
        re.search(r"\b(inbox\s+and\s+sent|sent\s+and\s+inbox|mailbox|all mail)\b", msg)
    )
    unread = bool(re.search(r"\bunread\b", msg))

    # If model already gave a usable Gmail query, keep it
    if q and any(
        tok in q.lower()
        for tok in ("in:", "from:", "to:", "subject:", "newer_than:", "is:", "label:")
    ):
        tag = "sent" if "in:sent" in q.lower() else ("inbox" if "in:inbox" in q.lower() else "custom")
        return q, tag

    # Keyword subject filter from user text
    subj = ""
    sm = re.search(
        r"\b(?:about|regarding|subject|filter(?:ed)?(?:\s+by)?|matching)\s+(.+)$",
        user_msg or "",
        re.I,
    )
    if sm:
        subj = sm.group(1).strip(" .\"'")
        # drop trailing ask words
        subj = re.split(r"\b(and then|then|please)\b", subj, maxsplit=1)[0].strip()

    parts: list[str] = []
    if want_both:
        # Caller should use extract_inbox_and_sent instead; return a marker
        return f"BOTH newer_than:{days}d", "both"
    if want_sent:
        parts.append("in:sent")
        tag = "sent"
    else:
        parts.append("in:inbox")
        tag = "inbox"
        if want_inbox:
            tag = "inbox"
    parts.append(f"newer_than:{days}d")
    if unread:
        parts.append("is:unread")
    if subj:
        # Gmail subject operator; quote multi-word
        if " " in subj:
            parts.append(f'subject:"{subj}"')
        else:
            parts.append(f"subject:{subj}")
    return " ".join(parts), tag


def _format_mailbox_digest(rows: list[dict[str, Any]], *, limit: int = 25) -> str:
    lines: list[str] = []
    for i, r in enumerate(rows[:limit], 1):
        box = r.get("mailbox") or "?"
        subj = (r.get("subject") or "(no subject)")[:80]
        who = r.get("from") if box != "sent" else (r.get("to") or r.get("from"))
        who = (who or "")[:60]
        date = (r.get("date") or "")[:32]
        mid = (r.get("message_id") or "").strip()
        id_bit = f" id=`{mid}`" if mid else ""
        lines.append(f"{i}. [{box}]{id_bit} | {date} | {who} | {subj}")
    if len(rows) > limit:
        lines.append(f"…and {len(rows) - limit} more.")
    return "\n".join(lines) or "(no messages)"


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


def _attachments_for_email(
    user_msg: str,
    chat_attachments: Optional[list[dict[str, Any]]],
) -> Optional[list[dict[str, Any]]]:
    """Only attach binary files when the user explicitly asks to include them.

    Staged uploads are always available as drafting *context*; they are not
    attached unless the user says attach/include the file.
    """
    if not chat_attachments:
        return None
    if not _wants_email_attachment(user_msg or ""):
        return None
    return _gmail_attachment_payload(chat_attachments)


def _extract_cc_emails(
    user_msg: str,
    *,
    seed: Optional[dict[str, Any]] = None,
    exclude: Optional[set[str]] = None,
    plan: Optional[IntentPlan] = None,
) -> list[str]:
    """Pull ALL CC addresses from seed, planner, and phrases like 'cc a@b.com and c@d.com'."""
    found: list[str] = []
    seed = seed or {}
    for key in ("cc", "cc_emails", "cc_email"):
        val = seed.get(key)
        if isinstance(val, str):
            found.extend(_EMAIL_RE.findall(val))
        elif isinstance(val, list):
            for item in val:
                found.extend(_EMAIL_RE.findall(str(item)))

    if plan and plan.cc:
        found.extend(plan.cc)

    roles = classify_email_roles(user_msg or "")
    found.extend(roles.cc)
    found.extend(default_cc_emails())

    exclude = {e.lower() for e in (exclude or set())}
    if plan:
        exclude |= {e.lower() for e in plan.ignore_emails}
        fe = (plan.from_email or default_from_email()).lower()
        if fe:
            exclude.add(fe)
    exclude.add("csr@karunamedia.org")

    out: list[str] = []
    seen: set[str] = set()
    for e in found:
        key = e.lower()
        if key in seen or key in exclude:
            continue
        seen.add(key)
        out.append(e)
    return out


def _mail_headers(
    user_msg: str,
    *,
    seed: Optional[dict[str, Any]] = None,
    to_emails: Optional[list[str]] = None,
    plan: Optional[IntentPlan] = None,
) -> dict[str, Any]:
    exclude = {e.lower() for e in (to_emails or [])}
    if plan:
        exclude |= {e.lower() for e in plan.ignore_emails}
    from_email = (
        (plan.from_email if plan and plan.from_email else None)
        or (seed or {}).get("from_email")
        or default_from_email()
    )
    cc = _extract_cc_emails(
        user_msg, seed=seed, exclude=exclude | {from_email.lower()}, plan=plan
    )
    # Ensure planner CCs are never dropped (even if a To scrape conflicted earlier)
    if plan and plan.cc:
        seen = {c.lower() for c in cc}
        for e in plan.cc:
            if (
                e.lower() not in seen
                and e.lower() not in exclude
                and e.lower() != from_email.lower()
            ):
                cc.append(e)
                seen.add(e.lower())
    return {"from_email": from_email, "cc": cc}


def _stamp_mail_fields(
    job: dict[str, Any],
    *,
    from_email: str,
    cc: list[str],
    attachments: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    job = {**job}
    job["from_email"] = from_email
    job["cc"] = list(cc or [])
    if attachments:
        job["attachments"] = attachments
    elif "attachments" in job and not attachments:
        job.pop("attachments", None)
    return job


def _want_gmail_signature() -> bool:
    try:
        from core.mail_prefs import include_gmail_signature

        return bool(include_gmail_signature())
    except Exception:
        return True


def _deliver_job(
    job: dict[str, Any],
    *,
    want_send: bool,
    user_msg: str,
) -> tuple[dict[str, Any], bool]:
    """Create a new draft (default) or send. Returns (result, did_send)."""
    do_send = want_send and not _prefer_draft_over_send(user_msg, True)
    title = (
        job.get("title")
        or job.get("designation")
        or job.get("recipient_title")
        or ""
    )
    first = str(job.get("first_name") or "").strip()
    if not first:
        name = str(job.get("recipient_name") or job.get("name") or "").strip()
        first = name.split(None, 1)[0] if name else ""
    html_body = _ensure_designation_in_greeting(
        job.get("html_body") or "",
        first_name=first,
        title=str(title),
    )
    try:
        from gmail_client.html_format import normalize_email_html

        html_body = normalize_email_html(html_body)
    except Exception:
        pass
    kwargs = {
        "to": job["recipient_email"],
        "subject": job.get("subject") or "(no subject)",
        "html_body": html_body,
        "recipient_name": job.get("recipient_name") or "",
        "recipient_title": str(title),
        "company": job.get("company") or "",
        "attachments": job.get("attachments"),
        "campaign": job.get("campaign"),
        "source": job.get("source"),
        "from_email": job.get("from_email") or default_from_email(),
        "cc": job.get("cc") or [],
        "bcc": job.get("bcc") or [],
        "include_signature": bool(
            job.get("include_signature")
            if "include_signature" in job
            else _want_gmail_signature()
        ),
    }
    if do_send:
        return send_email(**kwargs), True
    return create_draft(**kwargs, track=True), False


def _record_draft_preview(out: dict[str, Any], previews: list[dict[str, Any]]) -> str:
    """Store cleaned body for Chat/Drafts HTML preview; return a short status line."""
    cleaned = str(out.get("body_cleaned") or "")
    if not cleaned.strip() and out.get("body_html"):
        try:
            from gmail_client.html_format import prepare_draft_bodies

            cleaned, _html = prepare_draft_bodies(out.get("body_html") or "")
            out["body_cleaned"] = cleaned
        except Exception:
            cleaned = ""
    previews.append(
        {
            "subject": out.get("subject") or "",
            "to": out.get("to") or "",
            "cc": out.get("cc") or "",
            "body_cleaned": cleaned,
            "draft_id": out.get("draft_id") or "",
        }
    )
    did = out.get("draft_id") or "—"
    return f"Draft saved · draft_id=`{did}` · to {out.get('to') or '—'}\n"


def _run_styled_directive_draft(
    *,
    user_msg: str,
    directives: dict[str, Any],
    enrichment: Optional[dict[str, Any]],
    plan: IntentPlan,
    attachments: Optional[list[dict[str, Any]]],
    draft_previews: list[dict[str, Any]],
) -> Generator[str, None, None]:
    """One Gmail draft to directives['to'], optionally styled after template_from."""
    to = (directives.get("to") or "").strip()
    if not to:
        return
    template_from = (directives.get("template_from") or "").strip()
    if template_from and template_from.lower() == to.lower():
        yield (
            f"_Warning: `to` and style-template address are the same "
            f"(`{to}`). Proceeding anyway._\n"
        )
    style_ref = None
    if template_from:
        yield f"Looking up the most recent sent email to **{template_from}**…\n"
        style_ref = fetch_latest_sent_to(template_from)
        if not style_ref:
            yield (
                f"No sent messages found to `{template_from}`. "
                "Drafting without a style template.\n"
            )
        else:
            yield (
                f"_Loaded sent style · "
                f"**{(style_ref.get('subject') or '(no subject)').strip()}**_\n"
            )
    composed = compose_styled_email(
        to_email=to,
        enrichment=enrichment,
        style_template=style_ref,
        user_msg=user_msg,
    )
    model = composed.get("provider") or "gemini"
    yield f"_Chat model: **{model}**_\n"
    name = str((enrichment or {}).get("name") or "").strip()
    title = str((enrichment or {}).get("title") or "").strip()
    company = str((enrichment or {}).get("company") or "").strip()
    job = {
        "recipient_email": to,
        "recipient_name": name,
        "first_name": str((enrichment or {}).get("first_name") or "").strip()
        or (name.split(None, 1)[0] if name else ""),
        "title": title,
        "company": company,
        "subject": composed.get("subject") or "(no subject)",
        "html_body": composed.get("body_cleaned") or composed.get("html_body") or "",
        "from_email": plan.from_email or default_from_email(),
        "cc": list(directives.get("cc") or plan.cc or []),
        "bcc": list(directives.get("bcc") or []),
        "attachments": attachments,
        "source": "directive_draft",
    }
    ignore = {e.lower() for e in (directives.get("ignore") or [])}
    job["cc"] = [c for c in job["cc"] if c.lower() not in ignore]
    yield f"Creating draft to **{to}** from **{job['from_email']}**…\n"
    out, _did_send = _deliver_job(job, want_send=False, user_msg=user_msg)
    if out.get("error"):
        yield f"- Failed for {to}: {out.get('error')}\n"
        return
    yield _record_draft_preview(out, draft_previews)


def _lookup_enrichment_for(
    addr: str,
    prospects: Optional[list[dict[str, Any]]] = None,
    extra: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Enrichment for THIS address only — never fall back to another prospect."""
    addr_l = (addr or "").strip().lower()
    if not addr_l:
        return {}
    for p in list(extra or []) + list(prospects or []):
        if not isinstance(p, dict):
            continue
        if str(p.get("email") or "").strip().lower() == addr_l:
            return p
    try:
        from core.enrich_cache import _session_map

        sess = _session_map() or {}
        for data in sess.values():
            if (
                isinstance(data, dict)
                and str(data.get("email") or "").strip().lower() == addr_l
            ):
                return data
    except Exception:
        pass
    return {}


def _ask_who_to_draft(prospects: list[dict[str, Any]]) -> str:
    rows = _prospects_with_email(prospects)
    lines = [f"I found **{len(rows)}** prospects in this conversation:\n"]
    for i, p in enumerate(rows[:20], 1):
        name = (p.get("name") or "").strip() or "—"
        email = (p.get("email") or "").strip()
        lines.append(f"  {i}. {name} `<{email}>`\n")
    if len(rows) > 20:
        lines.append(f"  …and {len(rows) - 20} more.\n")
    lines.append(
        "\nWho should I draft to? Reply with the email address, "
        'or say **"draft to all"** to fan out.\n'
    )
    return "".join(lines)


def _collect_linkedin_profile_urls(
    user_msg: str,
    history: Optional[list[dict[str, str]]] = None,
    *,
    limit: int = 100,
) -> list[str]:
    """All unique LinkedIn /in/ URLs in this turn, else recent user pastes."""
    urls = extract_linkedin_urls(user_msg or "", limit=limit)
    if urls:
        return urls
    if not wants_linkedin_contact_lookup(user_msg or ""):
        return []
    for m in reversed(history or []):
        if str(m.get("role") or "").lower() != "user":
            continue
        urls = extract_linkedin_urls(str(m.get("content") or ""), limit=limit)
        if urls:
            return urls
    return []


def _allow_multi_provider(user_msg: str) -> bool:
    return bool(
        re.search(
            r"\b(?:apollo|rocketreach|rocket\s*reach|other providers?|"
            r"multi[- ]?provider|try\s+(?:apollo|rocketreach)|option\s*c)\b",
            user_msg or "",
            re.I,
        )
    )


def _enrich_linkedin_cached(
    url: str,
    ident: Optional[dict[str, Any]] = None,
    *,
    allow_multi: bool = False,
) -> dict[str, Any]:
    """ZoomInfo enrich for a LinkedIn URL; skip ZI when session cache already has it."""
    cached = get_cached_enrichment(url)
    if cached:
        return {**cached, "from_cache": True}
    payload = dict(ident or {})
    payload["linkedin_url"] = url
    if not payload.get("first_name") or not payload.get("last_name"):
        f, l = names_from_linkedin_url(url)
        payload.setdefault("first_name", f)
        payload.setdefault("last_name", l)
    result = enrich_fallthrough(
        payload,
        linkedin_url=url,
        allow_multi_provider=allow_multi,
    )
    if result and not result.get("error"):
        put_cached_enrichment(url, result)
    return result or {}


def _should_draft_after_prospect_search(
    user_msg: str,
    plan: IntentPlan,
    search_opts: Optional[dict[str, Any]] = None,
) -> bool:
    """True when PROSPECT_SEARCH should continue into like-sent / draft."""
    # Current message must ask for draft — ignore stale plan.like_sent_* from history
    if wants_search_then_draft(user_msg or ""):
        return True
    if parse_like_sent_request(user_msg or ""):
        return True
    if re.search(
        r"\b(?:and|then)\b.{0,40}\b(?:draft|compose|write|create\s+(?:an?\s+)?email)\b",
        user_msg or "",
        re.I | re.S,
    ):
        return True
    opts = search_opts or {}
    # Only trust opts when they came from THIS turn's attach_draft path
    if opts.get("draft") and (
        opts.get("like_sent_to")
        or opts.get("like_sent_message_id")
        or re.search(
            r"\b(draft|compose|write|create\s+(an?\s+)?email)\b",
            user_msg or "",
            re.I,
        )
    ):
        return True
    return False


def _draft_followup_message(user_msg: str, plan: IntentPlan) -> str:
    """Rewrite search+draft into a draft-only follow-up (avoids re-running ZoomInfo)."""
    like = parse_like_sent_request(user_msg or "") or {}
    ref = (
        (like.get("reference") or "").strip()
        or (plan.like_sent_to or "").strip()
        or ""
    )
    company = (
        (like.get("target") or "").strip()
        or (plan.like_sent_for or "").strip()
        or parse_contact_search_company(user_msg or "")
        or parse_explicit_draft_company(user_msg or "")
        or ""
    )
    if ref and company:
        return (
            f"draft emails to these prospects for {company} like {ref} to above"
        )
    if ref:
        return f"draft emails to these prospects like {ref} to above"
    if company:
        return f"draft personalized emails to these {company} prospects to above"
    return "draft personalized emails to all these prospects"


def _iter_draft_after_search(
    *,
    user_msg: str,
    history: Optional[list[dict[str, str]]],
    context: Optional[dict[str, Any]],
    plan: IntentPlan,
    prospects: list[dict[str, Any]],
) -> Generator[str | dict[str, Any], None, None]:
    """Continue into DRAFT_EMAIL using freshly searched contacts."""
    with_email = [p for p in prospects if (p.get("email") or "").strip()]
    if not with_email:
        yield (
            "\nNo emails on these contacts yet — cannot draft. "
            "Try enriching, or ask again after ZoomInfo returns emails.\n"
        )
        return
    dirs = parse_directives(user_msg or "")
    if dirs.get("explicit_recipient_lock"):
        draft_msg = user_msg or ""
        yield (
            f"\n**Next — drafting** to "
            f"**{', '.join(directive_to_list(dirs))}** "
            "(explicit address; not the full prospect list)…\n"
        )
    else:
        draft_msg = _draft_followup_message(user_msg, plan)
        yield (
            f"\n**Next — drafting** for **{len(with_email)}** contacts "
            f"(ZoomInfo search done)…\n"
        )
    draft_ctx = {
        **(context or {}),
        "prospects": with_email,
        "_after_prospect_search": True,
    }
    for chunk in answer(draft_msg, history=history, context=draft_ctx):
        if isinstance(chunk, dict) and "__meta__" in chunk:
            # Parent answer emits the final meta — skip nested meta
            continue
        # Skip nested plan summary noise — keep draft progress lines
        if isinstance(chunk, str) and chunk.startswith("**Plan:**"):
            continue
        yield chunk


def _reference_org_for_swap(
    like_ref: str,
    ref_msg: Optional[dict[str, Any]] = None,
) -> str:
    """Company/org label to replace in the cloned body (not the raw email)."""
    ref = (like_ref or "").strip()
    msg = ref_msg or {}
    if "@" in ref:
        to_hdr = str(msg.get("to") or "")
        m_name = re.match(r'\s*"?([^"<]+?)"?\s*<', to_hdr)
        if m_name:
            display = m_name.group(1).strip()
            if (
                display
                and "@" not in display
                and len(display) > 1
                and not _looks_like_person_name(display)
            ):
                return display
        return org_label_from_email(ref) or ref
    return ref or "prior organization"


def _looks_like_person_name(display: str) -> bool:
    """True for 'Khurshidalam Qureshi'; false for 'Magic Bus India'."""
    parts = [p for p in re.split(r"\s+", (display or "").strip()) if p]
    if len(parts) < 2:
        return False
    if re.search(
        r"\b(inc|ltd|llc|pvt|limited|foundation|trust|org|group|"
        r"corp|company|team|bus|media|technologies|tech)\b",
        display,
        re.I,
    ):
        return False
    return True


def _infer_like_sent_target(
    *,
    explicit: str,
    reference: str,
    prospects: Optional[list[dict[str, Any]]],
    history: Optional[list[dict[str, str]]],
    prefer_per_prospect: bool = False,
) -> str:
    """Pick the company to adapt the cloned email for.

    When drafting to a multi-company prospect list ("to above"), return "" so
    each recipient is personalized with their own company — never the Sent
    template org (e.g. Magic Bus).
    """
    target = (explicit or "").strip()
    ref_l = (reference or "").strip().lower()
    if target and target.lower() != ref_l and "@" not in target:
        return target
    if prefer_per_prospect:
        return ""
    companies: list[str] = []
    for p in prospects or []:
        c = (p.get("company") or "").strip()
        if not c:
            continue
        cl = c.lower()
        if cl == ref_l or (ref_l and ref_l in cl and "@" in (reference or "")):
            continue
        if "@" in (reference or "") and org_label_from_email(reference).lower() in cl:
            continue
        companies.append(c)
    uniq = list(dict.fromkeys(companies))
    if len(uniq) == 1:
        return uniq[0]
    # Multiple companies on the list → personalize per row, not one global swap
    if len(uniq) > 1:
        return ""
    # Last user/assistant turn may name a company after "for/about"
    if history:
        for m in reversed(history[-16:]):
            text = str(m.get("content") or "")
            fm = re.search(
                r"\b(?:for|about|targeting|company)\s+([A-Za-z0-9][A-Za-z0-9&.\'\- ]{2,50})",
                text,
                re.I,
            )
            if fm:
                cand = fm.group(1).strip(" .,;:")
                if cand.lower() != ref_l and "@" not in cand:
                    return cand
            # From prior like-sent planner lines
            m_for = re.search(
                r"like sent to:[^\n]*→\s*for\s+([^\n]+)",
                text,
                re.I,
            )
            if m_for:
                cand = m_for.group(1).strip()
                if cand.lower() != ref_l and "@" not in cand:
                    return cand
    return ""


def _reference_org_aliases(
    like_ref: str,
    ref_msg: Optional[dict[str, Any]],
    ref_org: str,
) -> list[str]:
    """All names/phrases to scrub from a cloned Magic Bus-style email."""
    msg = ref_msg or {}
    aliases: list[str] = []
    for raw in (
        ref_org,
        like_ref,
        org_label_from_email(like_ref) if "@" in (like_ref or "") else "",
    ):
        raw = (raw or "").strip()
        if raw:
            aliases.append(raw)
    if "@" in (like_ref or ""):
        try:
            domain = like_ref.split("@", 1)[1].strip().lower()
            aliases.append(domain)
            aliases.append(domain.split(".")[0])
        except Exception:
            pass
    to_hdr = str(msg.get("to") or "")
    m_name = re.match(r'\s*"?([^"<]+?)"?\s*<', to_hdr)
    if m_name:
        display = m_name.group(1).strip()
        if display and "@" not in display and len(display) > 1:
            aliases.append(display)
    body = _full_reference_text(
        str(msg.get("body_text") or ""),
        str(msg.get("body_html") or ""),
    )
    subj = str(msg.get("subject") or "")
    blob = f"{subj}\n{body}"
    for m in re.finditer(
        r"(?:Dear|Hi|Hello)\s+([A-Z][^,\n]{1,50}?)\s+Team\b",
        blob,
        re.I,
    ):
        aliases.append(m.group(1).strip())
    # Dedup longest-first later; keep order stable
    out: list[str] = []
    seen: set[str] = set()
    for a in aliases:
        key = a.lower()
        if len(a) < 2 or key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out


def _personalize_like_sent_job(
    *,
    subject: str,
    html_body: str,
    prospect: dict[str, Any],
    scrub_names: list[str],
) -> dict[str, str]:
    """Fill {company}/{first_name} and scrub template org (Magic Bus) → prospect company."""
    company = (
        str(prospect.get("company") or prospect.get("organization") or "").strip()
        or "your organization"
    )
    pctx = {**prospect, "company": company}
    subj = _apply_template(subject or "", pctx)
    body = _apply_template(html_body or "", pctx)
    for old in scrub_names:
        if not old or old.lower() in {"{company}", "your organization"}:
            continue
        if old.lower() == company.lower():
            continue
        subj = _replace_company_names(subj, old, company)
        body = _replace_company_names(body, old, company)
    # Greetings like "Dear Magic Bus Team" → Dear {First} / Dear {Company} team
    first = str(pctx.get("first_name") or "").strip()
    if not first:
        name = str(pctx.get("name") or "").strip()
        first = name.split(None, 1)[0] if name else ""
    title = (
        str(pctx.get("title") or "").strip()
        or str(pctx.get("designation") or "").strip()
    )
    greet_to = first or company
    body = re.sub(
        rf"(Dear|Hi|Hello)\s+{re.escape(company)}\s+Team\b",
        rf"\1 {greet_to}",
        body,
        flags=re.I,
    )
    body = _ensure_designation_in_greeting(body, first_name=first, title=title)
    return {"subject": subj, "html_body": body}

def _prospects_for_company(
    prospects: Optional[list[dict[str, Any]]],
    company: str,
) -> list[dict[str, Any]]:
    company_l = (company or "").strip().lower()
    rows = _prospects_with_email(prospects)
    if not company_l:
        return rows
    matched = [
        p
        for p in rows
        if company_l in (p.get("company") or "").lower()
        or (p.get("company") or "").lower() in company_l
    ]
    return matched or rows


def _extract_pasted_email(user_msg: str) -> str:
    """If the user pasted a full outreach email in chat, return that body."""
    msg = (user_msg or "").strip()
    if len(msg) < 800:
        return ""
    m = re.search(
        r"((?:Dear|Hi|Hello)\s+[^\n,]{1,80},?\s*\n[\s\S]{600,}?"
        r"(?:Thanks,?|Thank you|Best regards|Warm regards|Regards)\s*,?\s*(?:\n|$))",
        msg,
        re.I,
    )
    if not m:
        return ""
    pasted = m.group(1).strip()
    return pasted if len(pasted) >= 800 else ""


def _company_name_variants(company: str) -> list[str]:
    """Generate match variants for swapping a company name in copied email text."""
    name = (company or "").strip()
    if not name:
        return []
    variants = {name, name.lower(), name.upper(), name.title()}
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", name).strip()
    if spaced and spaced.lower() != name.lower():
        variants.update({spaced, spaced.lower(), spaced.title()})
    compact = re.sub(r"\s+", "", name)
    if compact:
        variants.add(compact)
        variants.add(compact.lower())
    # Domain-style labels: magicbusindia → Magic Bus / Magic Bus India
    low = re.sub(r"[^a-z0-9]+", "", name.lower())
    for brand, pretty in (
        ("magicbus", "Magic Bus"),
        ("indiamart", "IndiaMART"),
        ("sterlitetech", "Sterlite Tech"),
    ):
        if brand in low:
            variants.update({pretty, pretty.lower(), pretty.title()})
            if "india" in low and "india" not in pretty.lower():
                variants.update(
                    {
                        f"{pretty} India",
                        f"{pretty} India".lower(),
                    }
                )
    return sorted({v for v in variants if v}, key=len, reverse=True)


def _detect_company_phrases(text: str, company: str) -> list[str]:
    """Find longer legal/brand phrases like 'IndiaMART Intermesh' in the body."""
    if not text or not company:
        return []
    phrases: set[str] = set()
    for variant in _company_name_variants(company):
        if len(variant) < 3:
            continue
        pat = re.compile(
            rf"\b{re.escape(variant)}(?:\s+(?:Intermesh|Limited|Ltd\.?|Pvt\.?|Private|Inc\.?|LLC|Group|Corporation|Corp\.?))?\b",
            re.I,
        )
        for m in pat.finditer(text):
            phrases.add(m.group(0))
    # Always include base variants
    phrases.update(_company_name_variants(company))
    return sorted(phrases, key=len, reverse=True)


def _replace_company_names(
    text: str,
    old_company: str,
    new_company: str,
    *,
    extra_phrases: Optional[list[str]] = None,
) -> str:
    """Replace all old-company variants/phrases with new_company (longest first)."""
    if not text or not old_company or not new_company:
        return text or ""
    out = text
    phrases = list(extra_phrases or []) + _company_name_variants(old_company)
    # Dedupe, longest first
    seen: set[str] = set()
    ordered: list[str] = []
    for p in sorted(phrases, key=len, reverse=True):
        key = p.lower()
        if not p or key in seen:
            continue
        seen.add(key)
        ordered.append(p)
    for variant in ordered:
        out = re.sub(re.escape(variant), new_company, out, flags=re.I)
    return out


def _strip_email_noise(html: str) -> str:
    """Remove scripts/styles/tracking pixels; keep real content and original hrefs."""
    if not (html or "").strip():
        return ""
    try:
        from core.tracking import strip_tracking

        html = strip_tracking(html)
    except Exception:
        pass
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        for img in soup.find_all("img"):
            src = (img.get("src") or "").lower()
            if any(
                x in src
                for x in ("track", "/o?", "pixel", "open.gif", "spacer")
            ):
                img.decompose()
                continue
            w, h = img.get("width"), img.get("height")
            if str(w) in ("1", "0") and str(h) in ("1", "0"):
                img.decompose()
        return str(soup)
    except Exception:
        return re.sub(
            r"<style[\s\S]*?</style>|<script[\s\S]*?</script>",
            "",
            html,
            flags=re.I,
        )


def _full_reference_text(body_text: str, body_html: str) -> str:
    """Return the complete plain-text email body (no truncation, no tracking URLs)."""
    from core.tracking import strip_visible_tracking_urls

    text = strip_visible_tracking_urls((body_text or "").strip())
    html = _strip_email_noise(body_html or "")
    html_text = ""
    if html:
        try:
            from bs4 import BeautifulSoup

            html_text = BeautifulSoup(html, "html.parser").get_text("\n").strip()
        except Exception:
            html_text = html
        html_text = strip_visible_tracking_urls(html_text)
    # Prefer whichever source is more complete
    if len(html_text) > len(text) + 80:
        return html_text
    return text or html_text


def _linkify_plain(text: str) -> str:
    """Escape HTML, render **bold** / *italic*, then turn URLs into anchors."""
    from gmail_client.html_format import apply_inline_markdown

    return apply_inline_markdown(text or "", escape_html=True)


def _full_text_to_html(text: str) -> str:
    """Convert a full plain-text/markdown email to HTML (bold, lists, links)."""
    from gmail_client.html_format import plain_or_markdown_to_html

    return plain_or_markdown_to_html(text or "")


def _reference_still_present(text: str, reference_company: str) -> bool:
    if not text or not reference_company:
        return False
    compact = re.sub(r"[^a-z0-9]", "", reference_company.lower())
    if compact and len(compact) >= 4:
        if compact in re.sub(r"[^a-z0-9]", "", text.lower()):
            return True
    return False


def _adapt_why_section(
    full_text: str,
    *,
    reference_company: str,
    target_company: str,
    research_notes: str,
) -> str:
    """Optionally rewrite only the 'Why X specifically' block; leave rest intact."""
    if not research_notes.strip() or not full_text:
        return full_text
    m = re.search(
        r"(Why\s+[^\n]{0,80}\n)([\s\S]*?)(?=\n(?:Next step|Thank you|Thanks,?)\b)",
        full_text,
        re.I,
    )
    if not m:
        return full_text
    header, body = m.group(1), m.group(2).strip()
    if len(body) < 40:
        return full_text
    try:
        raw = extract_json(
            f"""Rewrite ONLY this "Why company specifically" email section for {target_company}.
Keep similar length and the same pitch structure. Use research facts.
Never mention {reference_company}. Keep it as plain paragraphs (no markdown).

Header line to keep (already renamed): {header.strip()}

Section body to rewrite:
{body}

Research:
{research_notes[:4000]}

Return JSON: {{"section_body": "...full rewritten section body only..."}}
""",
            system="Rewrite one email section only. Preserve length. JSON only.",
            max_tokens=2000,
        )
        data = json.loads(raw or "{}")
        new_body = str((data or {}).get("section_body") or "").strip()
        if new_body and len(new_body) >= int(len(body) * 0.7):
            new_body = _replace_company_names(
                new_body, reference_company, target_company
            )
            return full_text[: m.start(2)] + new_body + "\n\n" + full_text[m.end(2) :]
    except Exception as e:
        print(f"[router] why-section adapt failed: {e}", file=sys.stderr)
    return full_text


def _research_company_for_like_sent(
    *,
    company: str,
    ref_subj: str,
    ref_to: str,
    ref_body: str,
    user_msg: str,
) -> tuple[str, list[dict[str, Any]]]:
    """Gemini + Google Search alignment notes for one target company (never the template org)."""
    company = (company or "").strip()
    if not company or company in ("{company}",):
        return "", []
    try:
        notes, web_sources = grounded_collect(
            f"""Research {company} and how it aligns with
this prior outreach email's FULL content (offers, asks, themes).

Prior subject: {ref_subj}
Prior To: {ref_to}
Prior body:
{(ref_body or '')[:12000]}

User request: {user_msg}

Cover: what {company} does, relevant CSR/partnerships/programs, and concrete
ways each major point in the prior email maps to {company}. Be specific.
Do NOT write about the prior recipient organization as if it were the new target.
""",
            system=(
                "You are a careful company researcher. Use Google Search. "
                f"Focus only on {company} and how the outreach maps to them."
            ),
        )
        return (notes or ""), list(web_sources or [])
    except Exception as e:
        print(f"[router] like-sent research ({company}): {e}", file=sys.stderr)
        return "", []


def _compose_like_sent_for_company(
    *,
    user_msg: str,
    reference_msg: dict[str, Any],
    reference_company: str,
    scrub_names: list[str],
    target_company: str,
    research_notes: str,
    document_context: str = "",
) -> dict[str, str]:
    """Compose one company-personalized clone; scrub all template-org aliases."""
    target = (target_company or "").strip() or "{company}"
    composed = _compose_like_sent_email(
        user_msg=user_msg,
        reference_msg=reference_msg,
        reference_company=reference_company,
        target_company=target,
        research_notes=research_notes,
        document_context=document_context,
    )
    for old in scrub_names:
        if not old or old.lower() == target.lower():
            continue
        composed["html_body"] = _replace_company_names(
            composed.get("html_body") or "", old, target
        )
        composed["subject"] = _replace_company_names(
            composed.get("subject") or "", old, target
        )
    return composed


def _unique_prospect_companies(
    rows: list[dict[str, Any]],
    *,
    limit: int = 8,
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for p in rows:
        c = str(p.get("company") or p.get("organization") or "").strip()
        if not c:
            continue
        key = c.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
        if len(out) >= limit:
            break
    return out


def _compose_like_sent_email(
    *,
    user_msg: str,
    reference_msg: dict[str, Any],
    reference_company: str,
    target_company: str,
    research_notes: str,
    document_context: str = "",
) -> dict[str, str]:
    """Keep the FULL sent email; swap company names; lightly adapt Why-section only."""
    ref_subject = (reference_msg.get("subject") or "").strip()
    ref_body = (reference_msg.get("body_text") or "").strip()
    ref_html = (reference_msg.get("body_html") or "").strip()

    full_text = _full_reference_text(ref_body, ref_html)
    target = (target_company or "").strip() or "{company}"
    phrases = _detect_company_phrases(full_text + "\n" + ref_subject, reference_company)

    # 1) Deterministic full-body company swap — never summarize / never drop sections
    swapped = _replace_company_names(
        full_text,
        reference_company,
        target,
        extra_phrases=phrases,
    )
    swapped_subject = _replace_company_names(
        ref_subject,
        reference_company,
        target,
        extra_phrases=phrases,
    ) or ref_subject

    # 2) Optional: rewrite only "Why X specifically" using research (rest untouched)
    if research_notes.strip() and target and target != "{company}":
        swapped = _adapt_why_section(
            swapped,
            reference_company=reference_company,
            target_company=target,
            research_notes=research_notes,
        )

    # 3) If HTML source is richer structurally, swap names inside cleaned HTML instead
    html_out = ""
    cleaned_html = _strip_email_noise(ref_html)
    if cleaned_html and len(cleaned_html) > 200:
        html_swapped = _replace_company_names(
            cleaned_html,
            reference_company,
            target,
            extra_phrases=phrases,
        )
        # Prefer HTML only when it still contains essentially the full text
        try:
            from bs4 import BeautifulSoup

            html_plain = BeautifulSoup(html_swapped, "html.parser").get_text("\n")
        except Exception:
            html_plain = html_swapped
        if len(re.sub(r"\s+", "", html_plain)) >= int(
            len(re.sub(r"\s+", "", swapped)) * 0.9
        ):
            # Also apply why-section text into HTML path by rebuilding from swapped text
            # when why-section was adapted (swapped may differ from html_plain)
            if len(swapped) >= len(full_text) * 0.9:
                html_out = _full_text_to_html(swapped)
            else:
                html_out = html_swapped

    if not html_out:
        html_out = _full_text_to_html(swapped)

    # Final scrub for leftover reference company
    if _reference_still_present(html_out + swapped_subject, reference_company):
        html_out = _replace_company_names(
            html_out, reference_company, target, extra_phrases=phrases
        )
        swapped_subject = _replace_company_names(
            swapped_subject, reference_company, target, extra_phrases=phrases
        )
        swapped = _replace_company_names(
            swapped, reference_company, target, extra_phrases=phrases
        )

    # Drop cloned Gmail signature so append_signature adds it once later;
    # also render any leftover **markdown** inside HTML and hide tracking URLs.
    try:
        from gmail_client.html_format import (
            normalize_email_html,
            strip_trailing_signature_block,
        )
        from core.tracking import strip_tracking, strip_visible_tracking_urls

        html_out = strip_visible_tracking_urls(html_out)
        html_out = strip_tracking(html_out)
        html_out = strip_trailing_signature_block(html_out)
        html_out = normalize_email_html(html_out)
        html_out = strip_visible_tracking_urls(html_out)
    except Exception:
        pass

    alignment = ""
    if research_notes.strip():
        try:
            raw = extract_json(
                f"""In 1-2 sentences, why {target} fits this CSR outreach email.
Research:\n{research_notes[:3000]}
Return JSON: {{"alignment_summary":"..."}}""",
                system="Return JSON only.",
                max_tokens=300,
            )
            data = json.loads(raw or "{}")
            alignment = str((data or {}).get("alignment_summary") or "").strip()
        except Exception:
            alignment = ""

    return {
        "subject": swapped_subject or f"Partnership idea for {target}",
        "html_body": html_out,
        "alignment_summary": alignment,
        "paragraph_count": str(html_out.count("<p>")),
        "char_count": str(len(swapped)),
        "plain_preview": swapped[:1200],
    }


def _attach_note(
    attachments: Optional[list[dict[str, Any]]],
    *,
    used_document_context: bool = False,
    attached_to_email: bool = False,
) -> str:
    if not attachments:
        return ""
    names = ", ".join(a.get("name") or "file" for a in attachments)
    if attached_to_email:
        note = f" File attachment(s) included: {names}."
    elif used_document_context:
        note = (
            f" Used {names} as drafting context only "
            f"(not attached — say “attach the file” to include it)."
        )
    else:
        note = f" Files available: {names}."
    return note


def _resolve_recipient(
    job: dict[str, Any],
    user_msg: str,
    history: Optional[list[dict[str, str]]] = None,
    *,
    plan: Optional[IntentPlan] = None,
) -> dict[str, Any]:
    if job.get("recipient_email"):
        email = str(job["recipient_email"]).strip()
        if plan and email.lower() in plan.non_recipient_emails():
            job.pop("recipient_email", None)
        else:
            return job
    roles = classify_email_roles(user_msg or "")
    if roles.to:
        job["recipient_email"] = roles.to[0]
        return job
    if plan and plan.to_emails:
        job["recipient_email"] = plan.to_emails[0]
        return job
    blob = user_msg + "\n" + "\n".join(
        (m.get("content") or "") for m in (history or [])
    )
    found = _EMAIL_RE.findall(blob)
    block = plan.non_recipient_emails() if plan else {
        default_from_email().lower(),
        "csr@karunamedia.org",
        *[e.lower() for e in classify_email_roles(user_msg or "").cc],
        *[e.lower() for e in classify_email_roles(user_msg or "").ignore],
    }
    for e in reversed(found):
        if e.lower() not in block:
            job["recipient_email"] = e
            break
    return job


def route(user_msg: str, history: Optional[list[dict[str, str]]] = None) -> str:
    """Ask Gemini for a single-line routing decision."""
    messages = [{"role": "user", "content": user_msg}]
    if history:
        # Keep more turns so follow-ups resolve from chat, not hallucination
        trimmed = []
        for m in history[-12:]:
            content = (m.get("content") or "")[:1200]
            trimmed.append({"role": m.get("role") or "user", "content": content})
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
            "RESEARCH_THEN_ZOOM:",
            "GMAIL_EXTRACT:",
        ):
            if upper.startswith(prefix) or text.startswith(prefix):
                # Normalize to the canonical prefix casing from the first line start
                lines = text.splitlines()
                first = lines[0] if lines else text
                # If model wrapped JSON onto later lines, reassemble after first prefix
                if prefix.endswith("EMAIL:") or prefix.startswith("PROSPECT") or prefix.startswith("RESEARCH"):
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
      attachments: staged uploads (always available; user need not mention them)
      mailbox_messages: last inbox/sent pull from chat (for filters + follow-ups)
    """
    from core.chat_llm import preferred_provider, reset_provider, use_provider

    token = use_provider(preferred_provider())
    try:
        yield from _answer_impl(user_msg, history=history, context=context)
    finally:
        reset_provider(token)


def _answer_impl(
    user_msg: str,
    history: Optional[list[dict[str, str]]] = None,
    context: Optional[dict[str, Any]] = None,
) -> Generator[str | dict[str, Any], None, None]:
    prospects = (context or {}).get("prospects") or []
    chat_attachments = (context or {}).get("attachments") or []
    mailbox_messages = list((context or {}).get("mailbox_messages") or [])
    # Binary attach only when user asks; staged files still feed document context.
    email_atts = _attachments_for_email(user_msg, chat_attachments)
    doc_context = document_context_from_attachments(chat_attachments)
    used_docs = bool(doc_context.strip())
    att_names = _attachment_names(chat_attachments)
    consumed_attachments = False
    mailbox_out: list[dict[str, Any]] = []
    prospect_out: list[dict[str, Any]] = []
    cancelled = False
    draft_previews: list[dict[str, Any]] = []
    directives = parse_directives(user_msg or "")
    draft_debug: dict[str, Any] = {
        "user_message": user_msg or "",
        "parsed_directives": {
            "to": directives.get("to"),
            "to_list": directive_to_list(directives),
            "cc": directives.get("cc") or [],
            "bcc": directives.get("bcc") or [],
            "template_from": directives.get("template_from") or "",
            "linkedin_urls": directives.get("linkedin_urls") or [],
            "bulk_flag": bool(directives.get("bulk_flag")),
            "explicit_recipient_lock": bool(
                directives.get("explicit_recipient_lock")
            ),
        },
        "recipients_final": [],
        "draft_path": "",
        "ignored_count": len(
            [p for p in prospects if (p.get("email") or "").strip()]
        ),
    }
    if directives.get("attachments") and chat_attachments:
        wanted = {str(n).lower() for n in directives["attachments"]}
        extra = [
            a
            for a in chat_attachments
            if str(a.get("name") or "").lower() in wanted
            or any(w in str(a.get("name") or "").lower() for w in wanted)
        ]
        if extra:
            email_atts = extra
            consumed_attachments = True

    def _stop_now() -> bool:
        nonlocal cancelled
        if cancelled or is_cancelled(context):
            cancelled = True
            return True
        return False

    if _stop_now():
        yield stopped_message()
        yield {
            "__meta__": {
                "routing": "STOPPED",
                "sources": [],
                "cancelled": True,
            }
        }
        return

    routing = route(_route_user_msg(user_msg, chat_attachments), history)
    sources: list[dict[str, Any]] = []
    meta_routing = routing
    need_file = False

    # Intelligent planner: From/To/CC/ignore + which specialist agents to run
    if _stop_now():
        yield stopped_message()
        yield {"__meta__": {"routing": "STOPPED", "sources": [], "cancelled": True}}
        return
    plan = plan_request(user_msg, history)
    yield plan_summary(plan)
    if _stop_now():
        yield stopped_message()
        yield {"__meta__": {"routing": "STOPPED", "sources": [], "cancelled": True}}
        return

    # Map planner action onto routing when the classifier/heuristics would misfire
    action_to_prefix = {
        "research_then_zoom": "RESEARCH_THEN_ZOOM",
        "prospect_search": "PROSPECT_SEARCH",
        "prospect_enrich": "PROSPECT_ENRICH",
        "gmail_extract": "GMAIL_EXTRACT",
        "draft_email": "DRAFT_EMAIL",
        "send_email": "SEND_EMAIL",
        "schedule_email": "SCHEDULE_EMAIL",
        "memory": "MEMORY",
        "chat": "CHAT",
    }
    planned_prefix = action_to_prefix.get(plan.action, "CHAT")
    named_person = parse_named_person_contact(user_msg or "")
    # Named person lookup → enrich (internal first, then ZoomInfo)
    if named_person or planned_prefix == "PROSPECT_ENRICH":
        ident: dict[str, Any] = {}
        if routing.startswith("PROSPECT_ENRICH:"):
            ident = _parse_json_tail(routing, "PROSPECT_ENRICH:") or {}
            if not isinstance(ident, dict):
                ident = {}
        if named_person:
            ident = {**ident, **{k: v for k, v in named_person.items() if v}}
        if ident.get("company_domain") and not ident.get("company"):
            ident["company"] = str(ident["company_domain"]).split(".")[0]
        if ident:
            routing = "PROSPECT_ENRICH:" + json.dumps(ident, ensure_ascii=False)
            meta_routing = routing
    # Company-level contact search must never be overridden into a draft-only path
    # (search + like-sent still starts here, then continues to draft after ZoomInfo)
    elif (
        wants_contact_search(user_msg or "")
        or wants_search_then_draft(user_msg or "")
        or planned_prefix == "PROSPECT_SEARCH"
    ):
        company = parse_contact_search_company(user_msg or "") or (
            plan.like_sent_for or ""
        )
        q: dict[str, Any] = {
            "providers": ["zoominfo"],
            "limit": int(plan.search_limit or 25),
        }
        if company:
            q["company_names"] = [company]
        # Only attach draft/like-sent when THIS message asks for it — never from
        # leftover planner fields inherited from earlier chat turns.
        attach_draft = bool(
            wants_search_then_draft(user_msg or "")
            or parse_like_sent_request(user_msg or "")
            or (
                plan.draft
                and re.search(
                    r"\b(draft|compose|write|create\s+(an?\s+)?email)\b",
                    user_msg or "",
                    re.I,
                )
            )
        )
        if attach_draft:
            q["draft"] = True
            if plan.like_sent_to:
                q["like_sent_to"] = plan.like_sent_to
            if plan.like_sent_for or company:
                q["like_sent_for"] = plan.like_sent_for or company
            if plan.like_sent_message_id:
                q["like_sent_message_id"] = plan.like_sent_message_id
        # If classifier already produced PROSPECT_SEARCH JSON, merge company_names
        if routing.startswith("PROSPECT_SEARCH:"):
            existing = _parse_json_tail(routing, "PROSPECT_SEARCH:") or {}
            if isinstance(existing, dict):
                if company and not (
                    existing.get("company_names")
                    or existing.get("company")
                    or existing.get("companies")
                ):
                    existing["company_names"] = [company]
                existing.setdefault("providers", ["zoominfo"])
                # Strip inherited draft/like-sent unless this turn asked for them
                if not attach_draft:
                    for k in ("draft", "like_sent_to", "like_sent_for", "like_sent_message_id"):
                        existing.pop(k, None)
                q = {**q, **existing}
                if company:
                    q["company_names"] = [company]
                if attach_draft:
                    q["draft"] = True
        routing = "PROSPECT_SEARCH:" + json.dumps(q, ensure_ascii=False)
        meta_routing = routing
    # Prefer planner over RESEARCH_THEN_ZOOM false positives (CSR-as-sender → NGO list)
    elif planned_prefix == "DRAFT_EMAIL" and (
        routing.startswith("RESEARCH_THEN_ZOOM")
        or routing.startswith("PROSPECT_SEARCH")
        or routing == "CHAT"
        or routing.startswith("CHAT")
    ):
        seed_json: dict[str, Any] = {"batch": bool(plan.to_emails and len(plan.to_emails) > 1)}
        if len(plan.to_emails) == 1:
            seed_json["recipient_email"] = plan.to_emails[0]
        elif plan.to_emails:
            seed_json["recipient_emails"] = plan.to_emails
        if plan.cc:
            seed_json["cc"] = plan.cc
        if plan.ignore_emails:
            seed_json["ignore_emails"] = plan.ignore_emails
        if plan.like_sent_to:
            seed_json["like_sent_to"] = plan.like_sent_to
        if plan.like_sent_for:
            seed_json["like_sent_for"] = plan.like_sent_for
        if plan.like_sent_message_id:
            seed_json["like_sent_message_id"] = plan.like_sent_message_id
        if wants_prospect_list_recipients(user_msg or "") and not directives.get(
            "explicit_recipient_lock"
        ):
            seed_json["batch"] = True
            seed_json["from_prospects"] = True
        routing = "DRAFT_EMAIL:" + json.dumps(seed_json, ensure_ascii=False)
        meta_routing = routing
    elif (plan.like_sent_to or plan.like_sent_message_id) and (
        routing == "CHAT"
        or routing.startswith("CHAT")
        or routing.startswith("RESEARCH_THEN_ZOOM")
        or routing.startswith("GMAIL_EXTRACT")
    ):
        seed_json = {}
        if plan.like_sent_to:
            seed_json["like_sent_to"] = plan.like_sent_to
        if plan.like_sent_for:
            seed_json["like_sent_for"] = plan.like_sent_for
        if plan.like_sent_message_id:
            seed_json["like_sent_message_id"] = plan.like_sent_message_id
        if wants_prospect_list_recipients(user_msg or "") and not directives.get(
            "explicit_recipient_lock"
        ):
            seed_json["batch"] = True
            seed_json["from_prospects"] = True
        if len(plan.to_emails) == 1:
            seed_json["recipient_email"] = plan.to_emails[0]
        elif plan.to_emails:
            seed_json["recipient_emails"] = plan.to_emails
            seed_json["batch"] = True
        if plan.cc:
            seed_json["cc"] = plan.cc
        routing = (
            ("SEND_EMAIL:" if plan.send and not plan.draft else "DRAFT_EMAIL:")
            + json.dumps(seed_json, ensure_ascii=False)
        )
        meta_routing = routing
    elif planned_prefix == "RESEARCH_THEN_ZOOM" and (
        routing == "CHAT"
        or routing.startswith("CHAT")
        or routing.startswith("PROSPECT_SEARCH")
        or routing.startswith(("DRAFT_EMAIL", "SEND_EMAIL"))
    ):
        routing = "RESEARCH_THEN_ZOOM:" + json.dumps(
            {
                "org_limit": plan.org_limit,
                "contacts_per_org": plan.contacts_per_org,
                "email_limit": plan.email_limit,
                "draft": plan.draft or plan.send,
                "send": plan.send,
            },
            ensure_ascii=False,
        )
        meta_routing = routing
    elif planned_prefix == "GMAIL_EXTRACT" and (
        routing == "CHAT" or routing.startswith("CHAT")
    ):
        routing = "GMAIL_EXTRACT:auto"
        meta_routing = routing

    # Heuristic: force Gmail route when user clearly asks about mailbox
    if routing == "CHAT" or routing.startswith("CHAT"):
        if re.search(
            r"\b(my inbox|my sent|show (me )?(inbox|sent)|list (inbox|sent)|"
            r"unread (emails|mail)|filter (my )?(inbox|sent|mail)|"
            r"emails? (in|from) (my )?(inbox|sent))\b",
            user_msg or "",
            re.I,
        ):
            routing = "GMAIL_EXTRACT:auto"
            meta_routing = routing

    # Heuristic: LinkedIn URL(s) → enrich each on ZoomInfo (optionally draft/send)
    linkedin_urls = _collect_linkedin_profile_urls(
        user_msg or "", history, limit=min(max(int(plan.search_limit or 50), 1), 100)
    )
    linkedin_url = (linkedin_urls[0] if linkedin_urls else "") or extract_linkedin_url(
        user_msg or ""
    )
    wants_email_after_enrich = bool(
        (linkedin_url or linkedin_urls)
        and (
            bool(directives.get("to"))
            or wants_search_then_draft(user_msg or "")
            or re.search(
                r"\b(send|draft|email|mail|outreach|write (to|them|him|her)|"
                r"personaliz)\b",
                user_msg or "",
                re.I,
            )
        )
    )
    if (linkedin_urls or linkedin_url) and (
        routing == "CHAT"
        or routing.startswith("CHAT")
        or routing.startswith("PROSPECT_SEARCH")
        or routing.startswith("PROSPECT_ENRICH")
        or (
            wants_email_after_enrich
            and routing.startswith(("DRAFT_EMAIL", "SEND_EMAIL"))
        )
        or wants_linkedin_contact_lookup(user_msg or "")
    ):
        urls = linkedin_urls or ([linkedin_url] if linkedin_url else [])
        first, last = names_from_linkedin_url(urls[0]) if urls else ("", "")
        company_m = re.search(
            r"\b(?:at|@|company)\s+([A-Za-z0-9&.\- ]{2,60})",
            user_msg or "",
            re.I,
        )
        ident: dict[str, Any] = {
            "linkedin_url": urls[0] if urls else linkedin_url,
            "linkedin_urls": urls,
        }
        if first:
            ident["first_name"] = first
        if last:
            ident["last_name"] = last
        if company_m:
            ident["company"] = company_m.group(1).strip(" .,")
        routing = "PROSPECT_ENRICH:" + json.dumps(ident, ensure_ascii=False)
        meta_routing = routing

    # Heuristic: mission org discovery → web → ZoomInfo → optional draft
    # Skipped when planner already chose draft/CSR-as-sender
    if (
        plan.action == "research_then_zoom"
        and wants_research_then_zoom(user_msg or "")
        and (
            routing == "CHAT"
            or routing.startswith("CHAT")
            or routing.startswith("PROSPECT_SEARCH")
            or routing.startswith(("DRAFT_EMAIL", "SEND_EMAIL"))
        )
    ):
        routing = "RESEARCH_THEN_ZOOM:" + json.dumps(
            {
                "org_limit": plan.org_limit,
                "contacts_per_org": plan.contacts_per_org,
                "draft": plan.draft
                or bool(
                    re.search(r"\b(draft|write|compose|personaliz|email|outreach)\b", user_msg or "", re.I)
                ),
                "send": plan.send,
            },
            ensure_ascii=False,
        )
        meta_routing = routing

    # Guard: never run RESEARCH_THEN_ZOOM for CSR-as-sender drafts
    if routing.startswith("RESEARCH_THEN_ZOOM") and plan.action in (
        "draft_email",
        "send_email",
        "schedule_email",
        "chat",
    ):
        if not wants_research_then_zoom(user_msg or ""):
            seed_json = {}
            if plan.to_emails:
                if len(plan.to_emails) == 1:
                    seed_json["recipient_email"] = plan.to_emails[0]
                else:
                    seed_json["recipient_emails"] = plan.to_emails
                    seed_json["batch"] = True
            if plan.cc:
                seed_json["cc"] = plan.cc
            fallback: dict[str, Any] = {}
            if directives.get("explicit_recipient_lock"):
                locked = directive_to_list(directives)
                if len(locked) == 1:
                    fallback["recipient_email"] = locked[0]
                elif locked:
                    fallback["recipient_emails"] = locked
                    fallback["batch"] = True
            else:
                fallback = {"batch": True, "from_prospects": True}
            routing = (
                ("SEND_EMAIL:" if plan.send and not plan.draft else "DRAFT_EMAIL:")
                + json.dumps(seed_json or fallback, ensure_ascii=False)
            )
            meta_routing = routing
    try:
        # Email attach requested but no file staged → ask for upload (don't send yet)
        if (
            not chat_attachments
            and _wants_email_attachment(user_msg)
            and routing.startswith(("DRAFT_EMAIL", "SEND_EMAIL", "SCHEDULE_EMAIL"))
        ):
            need_file = True
            yield _ask_for_upload(for_email_attach=True)
            yield {
                "__meta__": {
                    "routing": meta_routing,
                    "sources": [],
                    "consumed_attachments": False,
                    "need_file": True,
                    "pending_user_msg": user_msg,
                }
            }
            return

        # General context from a file requested but nothing staged
        if (
            not chat_attachments
            and _wants_file_context(user_msg)
            and not routing.startswith(
                ("DRAFT_EMAIL", "SEND_EMAIL", "SCHEDULE_EMAIL", "PROSPECT_", "GMAIL_")
            )
        ):
            need_file = True
            yield _ask_for_upload(for_email_attach=False)
            yield {
                "__meta__": {
                    "routing": "CHAT_NEED_FILE",
                    "sources": [],
                    "consumed_attachments": False,
                    "need_file": True,
                    "pending_user_msg": user_msg,
                }
            }
            return

        if routing.startswith("MEMORY"):
            hits = mem.search(user_msg, k=8)
            system = chat_grounding_system(
                history=history,
                prospects=prospects,
                mailbox_messages=mailbox_messages,
                memory_hits=hits,
                document_context=doc_context,
                attachment_names=att_names or None,
            )
            system = (
                "Answer using saved memory + chat/session context. "
                "Cite memory with [n] when using saved notes. "
                "Do not invent facts not present in memory or chat.\n\n"
                + system
            )
            for chunk in chat_grounded(
                user_msg, history=history, system=system, use_search=False
            ):
                if isinstance(chunk, dict) and "__meta__" in chunk:
                    sources = chunk["__meta__"].get("sources") or []
                else:
                    yield chunk

        elif routing.startswith("RESEARCH_THEN_ZOOM"):
            opts = _parse_json_tail(routing, "RESEARCH_THEN_ZOOM:") or {}
            vol = parse_research_limits(user_msg)
            org_limit = int(
                opts.get("org_limit") or plan.org_limit or vol["org_limit"]
            )
            per_org = int(
                opts.get("contacts_per_org")
                or plan.contacts_per_org
                or vol["contacts_per_org"]
            )
            email_cap = int(
                opts.get("email_limit") or plan.email_limit or vol["email_limit"] or MAX_EMAILS
            )
            org_limit = max(org_limit, vol["org_limit"])
            per_org = max(per_org, vol["contacts_per_org"])
            email_cap = min(max(email_cap, vol["email_limit"]), MAX_EMAILS)
            do_draft = bool(opts.get("draft"))
            do_send = bool(opts.get("send"))
            if re.search(r"\b(draft|write|compose|personaliz|email|outreach)\b", user_msg or "", re.I):
                do_draft = True
            if re.search(r"\b(send now|email them now|fire off)\b", user_msg or "", re.I) and not re.search(
                r"\bdraft\b", user_msg or "", re.I
            ):
                do_send = True

            yield (
                f"**Step 1/3 — Web research:** finding up to **{org_limit}** NGOs/orgs "
                f"that match your mission filters "
                f"({per_org} contacts/org, ≤{email_cap} emails)…\n"
            )
            try:
                orgs, web_sources, _notes = discover_orgs_from_web(
                    user_msg, limit=org_limit
                )
            except Exception as e:
                print(f"[router] research discover error: {e}", file=sys.stderr)
                yield f"Web research failed: {e}\n"
                orgs, web_sources = [], []

            sources.extend(web_sources or [])
            if _stop_now():
                yield stopped_message()
                orgs = []

            if orgs:
                yield f"\nFound **{len(orgs)}** matching organizations:\n"
                for i, org in enumerate(orgs, 1):
                    yield (
                        f"{i}. **{org.get('name')}**"
                        + (f" — {org.get('website')}" if org.get("website") else "")
                        + (f" · {org.get('location')}" if org.get("location") else "")
                        + (
                            f"\n   Focus: {org.get('focus')}"
                            if org.get("focus")
                            else ""
                        )
                        + "\n"
                    )
            else:
                yield "\nNo concrete organizations extracted from web research.\n"

            yield (
                "\n**Step 2/3 — ZoomInfo one-by-one:** for each NGO, search ZoomInfo "
                "and enrich **email + mobile**, then try public contacts if needed…\n"
            )
            contacts: list[dict[str, Any]] = []
            for event in iter_enrich_orgs_on_zoominfo(
                orgs,
                contacts_per_org=per_org,
                web_email_fallback=True,
                cancel_check=_stop_now,
            ):
                if event.get("type") == "cancelled":
                    yield stopped_message()
                    break
                if event.get("type") != "org":
                    continue
                result = event.get("result") or {}
                org = result.get("org") or {}
                org_contacts = result.get("contacts") or []
                contacts.extend(org_contacts)
                idx = event.get("index")
                total = event.get("total")
                we = int(result.get("with_email") or 0)
                wm = int(result.get("with_mobile") or 0)
                people_n = sum(1 for c in org_contacts if not c.get("research_only"))
                yield (
                    f"\n**[{idx}/{total}] {org.get('name') or 'Org'}** — "
                    f"{people_n} contact(s), **{we}** email, **{wm}** mobile"
                )
                note_bits = result.get("notes") or []
                if note_bits:
                    yield f" · _{'; '.join(note_bits)}_"
                yield "\n"
                for c in org_contacts:
                    if c.get("research_only"):
                        continue
                    email = (c.get("email") or "").strip() or "—"
                    mobile = (
                        (c.get("mobile") or c.get("phone") or "").strip() or "—"
                    )
                    li = (c.get("linkedin_url") or "").strip()
                    yield (
                        f"- {c.get('name') or 'Contact'}"
                        + (f" · {c.get('title')}" if c.get("title") else "")
                        + f" · `{email}` · 📱 `{mobile}`"
                        + (f" · {li}" if li else "")
                        + "\n"
                    )
                if _stop_now():
                    yield stopped_message()
                    break

            with_email = [c for c in contacts if (c.get("email") or "").strip()]
            with_mobile = [
                c
                for c in contacts
                if (c.get("mobile") or c.get("phone") or "").strip()
            ]
            research_only = [c for c in contacts if c.get("research_only")]
            people = [c for c in contacts if not c.get("research_only")]
            prospect_out = people or contacts

            yield (
                f"\n**ZoomInfo summary:** **{len(people)}** contacts · "
                f"**{len(with_email)}** with email · **{len(with_mobile)}** with mobile"
            )
            if research_only:
                yield (
                    f" · _{len(research_only)} orgs without people "
                    f"(saved for later enrich)_"
                )
            yield "\n"

            try:
                from core.prospect_list import save_prospects

                research_clean = [
                    c for c in prospect_out if not c.get("research_only") and not c.get("error")
                ]
                list_n = save_prospects(research_clean)
                auto_ingest_prospects(research_clean)
                yield (
                    f"\nAuto-saved **{list_n or len(research_clean)}** contacts to your "
                    f"**Drive prospect list** "
                    f"(reused next time when email is already on file; "
                    f"missing email auto-checks ZoomInfo).\n"
                )
            except Exception as e:
                print(f"[router] research auto-ingest error: {e}", file=sys.stderr)
                yield f"\n⚠️ Prospect list save issue: {e}\n"

            # Step 3: personalized drafts for contacts with email (review before send)
            if (do_draft or do_send) and with_email:
                if directives.get("explicit_recipient_lock"):
                    locked = {a.lower() for a in directive_to_list(directives)}
                    with_email = [
                        c
                        for c in with_email
                        if str(c.get("email") or "").strip().lower() in locked
                    ]
                    if not with_email:
                        # Still draft the named address(es) without search-row context
                        with_email = [
                            {"email": a} for a in directive_to_list(directives)
                        ]
                # Always draft unless user clearly says send now
                want_send = bool(do_send) and not _prefer_draft_over_send(
                    user_msg, True
                )
                yield (
                    f"\n**Step 3/3 — "
                    f"{'Sending' if want_send else 'Creating new Gmail drafts'}** "
                    f"to {len(with_email)} contacts "
                    f"(from **{default_from_email()}**, with your signature)…\n"
                )
                # Build intent-aware template via JSON extract
                intent = extract_json(
                    f"""Create a short personalized outreach email template for this request.

User request:
{user_msg}

Use placeholders exactly: {{first_name}}, {{name_with_title}}, {{company}}, {{title}}, {{org_focus}}
Greet with first name only — never put title in parentheses (not "Hi {{first_name}} ({{title}})").
Return JSON:
{{"subject":"...","html_body":"<p>Hi {{first_name}},</p>..."}}

Keep it warm, specific to girls/skilling NGO partnership if relevant.
If user mentions karunamedia.org or a brand, reference collaboration politely.
HTML only in html_body. No markdown. Do not include a signature block.
""",
                    system="Return JSON only for an email template.",
                    max_tokens=900,
                )
                try:
                    tmpl = json.loads(intent or "{}")
                except Exception:
                    tmpl = {}
                subject = (
                    tmpl.get("subject")
                    or "Partnership idea for {company}'s girls skilling work"
                )
                html_body = tmpl.get("html_body") or (
                    "<p>Hi {first_name},</p>"
                    "<p>I came across <strong>{company}</strong> and your work "
                    "on {org_focus}.</p>"
                    "<p>I'd love to explore a collaboration that supports "
                    "skilling opportunities for girls aged 16+.</p>"
                    "<p>Would you be open to a short call next week?</p>"
                    "<p>Best regards</p>"
                )

                headers = _mail_headers(
                    user_msg,
                    to_emails=[c.get("email") for c in with_email],
                    plan=plan,
                )
                payload = {
                    "batch": True,
                    "from_prospects": True,
                    "subject": subject,
                    "html_body": html_body,
                    "source": "research_then_zoom",
                    "from_email": headers["from_email"],
                    "cc": headers["cc"],
                    "ignore_emails": plan.ignore_emails,
                }
                if email_atts:
                    payload["attachments"] = email_atts
                    consumed_attachments = True
                jobs = _build_draft_jobs(
                    payload,
                    user_msg,
                    history=history,
                    prospects=with_email,
                    mailbox_messages=mailbox_messages,
                    plan=plan,
                )
                if len(jobs) > email_cap:
                    yield (
                        f"\n_Capping at **{email_cap}** emails "
                        f"(found {len(jobs)}; say a higher number up to 100 if needed)._\n"
                    )
                    jobs = apply_email_cap(jobs, email_limit=email_cap)
                ok_n = 0
                fail_n = 0
                for job in jobs:
                    if _stop_now():
                        yield stopped_message()
                        break
                    job = _stamp_mail_fields(
                        job,
                        from_email=headers["from_email"],
                        cc=headers["cc"],
                        attachments=email_atts,
                    )
                    try:
                        out, did_send = _deliver_job(
                            job, want_send=want_send, user_msg=user_msg
                        )
                        if out.get("error"):
                            fail_n += 1
                            yield (
                                f"- Failed → **{job.get('recipient_email')}**: "
                                f"{out.get('error')}\n"
                            )
                            continue
                        ok_n += 1
                        if not did_send:
                            yield _record_draft_preview(out, draft_previews)
                        cc_note = f" cc {out.get('cc')}" if out.get("cc") else ""
                        did = f"draft_id={out.get('draft_id')}" if out.get("draft_id") else ""
                        yield (
                            f"- {'Sent' if did_send else 'Drafted'} → "
                            f"**{job.get('recipient_email')}** "
                            f"(from {out.get('from') or headers['from_email']}{cc_note}"
                            + (f"; {did}" if did else "")
                            + ")\n"
                        )
                    except Exception as e:
                        fail_n += 1
                        yield f"- Failed {job.get('recipient_email')}: {e}\n"
                yield (
                    f"\nDone: **{ok_n}** "
                    f"{'emails sent' if want_send else 'new tracked drafts for review'}"
                    + (f", **{fail_n}** failed" if fail_n else "")
                    + ".\n"
                )
                if ok_n and not want_send:
                    yield (
                        "Open **Drafts** (or Gmail → Drafts) to review, then send.\n"
                    )
                if chat_attachments and not email_atts:
                    yield (
                        _attach_note(
                            chat_attachments,
                            used_document_context=used_docs,
                            attached_to_email=False,
                        )
                        + "\n"
                    )
            elif do_draft or do_send:
                yield (
                    "\nNo ZoomInfo emails available to draft yet. "
                    "Try enriching a LinkedIn URL, or ask me to draft once contacts "
                    "with emails appear.\n"
                )
            else:
                yield (
                    "\nNext: `draft personalized emails to all these prospects` "
                    "about your partnership / skilling program.\n"
                )

            system = (
                "Summarize the research→ZoomInfo pipeline for the user. "
                "List org names, which contacts have emails, and remind them "
                "demographics (girls 16+) were used for WEB research only — "
                "ZoomInfo only stores org staff contacts.\n\n"
                f"Organizations:\n{json.dumps(orgs, default=str)[:4000]}\n\n"
                f"Contacts:\n{json.dumps(people[:40], default=str)[:8000]}"
            )
            if used_docs:
                system += "\n\nUploaded file context:\n" + doc_context
            for chunk in chat_grounded(
                user_msg, history=history, system=system, use_search=False
            ):
                if isinstance(chunk, dict) and "__meta__" in chunk:
                    more = chunk["__meta__"].get("sources") or []
                    if more:
                        sources.extend(more)
                else:
                    yield chunk
            sources.append(
                {
                    "title": "research_then_zoom",
                    "url": "",
                    "type": "pipeline",
                    "orgs": len(orgs),
                    "contacts": len(people),
                    "with_email": len(with_email),
                }
            )

        elif routing.startswith("PROSPECT_SEARCH:"):
            q = _parse_json_tail(routing, "PROSPECT_SEARCH:")
            if not q:
                q = {"keywords": user_msg}
            # Provider selection: ZoomInfo only (Apollo / RocketReach disabled)
            providers = q.pop("providers", None) or q.pop("provider", None)
            if isinstance(providers, str):
                providers = [providers]
            # Always ZoomInfo — ignore apollo/rocketreach even if classifier asks
            providers = ("zoominfo",)
            vol = parse_research_limits(user_msg)
            limit = int(
                q.pop("limit", None)
                or q.pop("limit_per_provider", None)
                or plan.search_limit
                or vol["search_limit"]
                or DEFAULT_SEARCH_LIMIT
            )
            limit = max(limit, vol["search_limit"])
            limit = min(max(limit, 1), 100)

            from core.prospect_list import (
                count_with_email,
                email_blocked_for_company_search,
                enough_emailed_contacts,
                filter_prospects_for_company_query,
                lookup_for_query,
                wants_force_refresh,
            )

            # "search contact from Sterlite Tech" must hit ZoomInfo — local list
            # is only a fallback when the user asks for saved/memory, or when a
            # non-explicit query already has enough emailed contacts on file.
            live_zoom = wants_live_zoominfo_search(user_msg or "") or wants_force_refresh(
                user_msg or ""
            )

            companies_q: list[str] = []
            for key in ("company_names", "companies", "company"):
                val = q.get(key)
                if isinstance(val, list):
                    companies_q.extend(str(x) for x in val if x)
                elif isinstance(val, str) and val.strip():
                    companies_q.append(val.strip())

            cached: list[dict[str, Any]] = []
            if not wants_force_refresh(user_msg or ""):
                cached = lookup_for_query(q, limit=limit)
                # Drop IndiaMART/etc. rows polluted onto this company name
                cached = filter_prospects_for_company_query(cached, companies_q)

            specific = bool(
                companies_q
                or q.get("company_domains")
                or q.get("domains")
            )
            # Only skip ZoomInfo when we already have enough contacts *with email*
            # AND the user did not ask for a live contact search.
            use_saved = bool(
                not live_zoom
                and cached
                and enough_emailed_contacts(cached, limit=limit, specific=specific)
            )

            if use_saved:
                ok = cached[:limit]
                errs: list[dict[str, Any]] = []
                prospect_out = ok
                saved_ids: list[str] = []
                try:
                    from core.prospect_list import save_prospects

                    kept_n = save_prospects(ok)
                except Exception as e:
                    print(f"[router] list re-save error: {e}", file=sys.stderr)
                    kept_n = 0
                try:
                    saved_ids = auto_ingest_prospects(ok)
                except Exception as e:
                    print(f"[router] list re-save ingest error: {e}", file=sys.stderr)
                    saved_ids = []
                ctx_lines = [f"{i}. {prospect_to_text(p)}" for i, p in enumerate(ok[:100], 1)]
                with_email = count_with_email(ok)
                yield (
                    f"Using **{len(ok)}** saved contacts from your prospect list "
                    f"(**{with_email}** with email).\n\n"
                )
                yield "\n".join(ctx_lines)
                if kept_n or saved_ids:
                    yield (
                        f"\n\nKept **{kept_n or len(ok)}** contacts on your Drive list."
                    )
                draft_after = _should_draft_after_prospect_search(
                    user_msg or "", plan, q
                ) and not (context or {}).get("_after_prospect_search")
                if draft_after:
                    for chunk in _iter_draft_after_search(
                        user_msg=user_msg or "",
                        history=history,
                        context=context,
                        plan=plan,
                        prospects=ok,
                    ):
                        yield chunk
                else:
                    yield (
                        "\n\nNext: `draft emails to all these prospects` or "
                        "`send personalized emails to this list`."
                    )
                sources.append(
                    {
                        "title": "prospect_list",
                        "url": "",
                        "type": "prospects",
                        "count": len(ok),
                        "cached": True,
                    }
                )
            else:
                if cached and not use_saved:
                    yield (
                        "Saved list incomplete — checking ZoomInfo for contact "
                        "details (email/mobile)…\n"
                    )
                yield (
                    f"Searching **{', '.join(providers)}** "
                    f"(ZoomInfo CSR emails first; if none, Google CSR Head email "
                    f"+ LinkedIn → ZoomInfo; then broader contacts; "
                    f"limit **{limit}**)…\n"
                )
                results = search_all(
                    q, providers=tuple(providers), limit_per_provider=limit
                )
                ok = [p for p in results if not p.get("error")]
                # Ensure ZoomInfo rows are tagged for the Saved list
                for p in ok:
                    if not (p.get("source") or "").strip():
                        p["source"] = "zoominfo"
                    elif str(p.get("source") or "").lower() in ("zi", "zoom", "zoom info"):
                        p["source"] = "zoominfo"
                # Never merge Saved-list pollution into a live ZoomInfo search.
                # (Earlier bug: IndiaMART like-sent email tagged company=Sterlite
                # was appended here and shown as the only "ZoomInfo" hit.)
                if companies_q:
                    ok = [
                        p
                        for p in ok
                        if not email_blocked_for_company_search(
                            str(p.get("email") or "")
                        )
                    ]
                errs = [p for p in results if p.get("error")]
                prospect_out = ok
                saved_ids = []
                list_saved = 0
                if ok:
                    try:
                        from core.prospect_list import save_prospects

                        # Explicit Drive upsert — do not rely on memory ingest alone
                        list_saved = save_prospects(ok)
                    except Exception as e:
                        print(
                            f"[router] prospect_list save error: {e}",
                            file=sys.stderr,
                        )
                    try:
                        saved_ids = auto_ingest_prospects(ok)
                    except Exception as e:
                        print(
                            f"[router] auto-ingest prospects error: {e}",
                            file=sys.stderr,
                        )
                ctx_lines = []
                for i, p in enumerate(ok[:100], 1):
                    ctx_lines.append(f"{i}. {prospect_to_text(p)}")
                if len(ok) > 100:
                    ctx_lines.append(f"…and {len(ok) - 100} more.")
                for e in errs[:5]:
                    ctx_lines.append(f"ERROR [{e.get('source')}]: {e.get('error')}")
                if not ok:
                    yield (
                        "No prospects returned. "
                        + (
                            f"Errors: {errs}"
                            if errs
                            else "Try a clearer title/company."
                        )
                    )
                else:
                    with_email = sum(1 for p in ok if (p.get("email") or "").strip())
                    yield (
                        f"Found **{len(ok)}** contacts "
                        f"(**{with_email}** with email) via {', '.join(providers)}.\n\n"
                    )
                    yield "\n".join(ctx_lines)
                    if list_saved or saved_ids:
                        yield (
                            f"\n\nAuto-saved **{list_saved or len(ok)}** contacts to your "
                            f"**Prospects → Saved** list"
                            + (
                                f" · memory ids {len(saved_ids)}"
                                if saved_ids
                                else ""
                            )
                            + ". Open **🎯 Prospects** to view them."
                        )
                    else:
                        yield (
                            "\n\n⚠️ Could not auto-save to Drive — check "
                            "`BOOTSTRAP_TOKEN_JSON` / `RELAY_DRIVE_FOLDER_ID`."
                        )
                    draft_after = _should_draft_after_prospect_search(
                        user_msg or "", plan, q
                    ) and not (context or {}).get("_after_prospect_search")
                    if draft_after:
                        for chunk in _iter_draft_after_search(
                            user_msg=user_msg or "",
                            history=history,
                            context=context,
                            plan=plan,
                            prospects=ok,
                        ):
                            yield chunk
                    else:
                        yield (
                            "\n\nNext: `draft emails to all these prospects` or "
                            "`send personalized emails to this ZoomInfo list`."
                        )
                # Do NOT run a second Gemini pass over ZoomInfo hits — it can invent
                # extra emails and Chat then has nothing structured to put in Saved.
                sources.append(
                    {
                        "title": "prospect_search",
                        "url": "",
                        "type": "prospects",
                        "count": len(ok),
                        "providers": list(providers),
                    }
                )

        elif routing.startswith("PROSPECT_ENRICH:"):
            ident = _parse_json_tail(routing, "PROSPECT_ENRICH:") or {}
            raw_urls = ident.get("linkedin_urls") or []
            batch_urls: list[str] = []
            if isinstance(raw_urls, str):
                batch_urls = extract_linkedin_urls(raw_urls)
            elif isinstance(raw_urls, list):
                seen_li: set[str] = set()
                for u in raw_urls:
                    for got in extract_linkedin_urls(str(u)):
                        key = got.lower().rstrip("/")
                        if key not in seen_li:
                            seen_li.add(key)
                            batch_urls.append(got)
            if not batch_urls:
                batch_urls = _collect_linkedin_profile_urls(
                    user_msg or "", history
                )
            did_batch = False
            if len(batch_urls) > 1:
                did_batch = True
                yield (
                    f"Found **{len(batch_urls)}** LinkedIn profiles — "
                    f"looking each up on **ZoomInfo** one by one…\n"
                )
                results: list[dict[str, Any]] = []
                for i, url in enumerate(batch_urls, 1):
                    if _stop_now():
                        yield stopped_message()
                        break
                    first, last = names_from_linkedin_url(url)
                    label = " ".join(x for x in (first, last) if x) or url
                    yield f"**{i}/{len(batch_urls)}** ZoomInfo · {label}…\n"
                    one = _enrich_linkedin_cached(
                        url,
                        {
                            "linkedin_url": url,
                            "first_name": first,
                            "last_name": last,
                        },
                        allow_multi=_allow_multi_provider(user_msg or ""),
                    )
                    if one.get("from_cache"):
                        yield "_Cached (no ZoomInfo credit used)._\n"
                    if one and not one.get("error"):
                        results.append(one)
                        yield format_enrichment_panel(one) + "\n"
                    else:
                        err = (one or {}).get("error") or "no match"
                        yield f"- No ZoomInfo match for **{label}** ({err})\n"
                if results:
                    try:
                        from core.prospect_list import save_prospects

                        saved_n = save_prospects(results)
                        auto_ingest_prospects(results)
                        yield (
                            f"\nSaved **{saved_n or len(results)}** contacts "
                            f"to **🎯 Prospects**.\n"
                        )
                    except Exception as e:
                        print(f"[router] linkedin batch save: {e}", file=sys.stderr)
                        try:
                            auto_ingest_prospects(results)
                        except Exception:
                            pass
                with_email_n = sum(
                    1 for p in results if (p.get("email") or "").strip()
                )
                prospect_out = results
                yield (
                    f"Done: **{len(results)}** matched · "
                    f"**{with_email_n}** with email "
                    f"(of {len(batch_urls)} profiles).\n"
                )
                sources.append(
                    {
                        "title": "linkedin_zoominfo_batch",
                        "url": "",
                        "type": "prospects",
                        "count": len(results),
                    }
                )
                if (
                    directives.get("to")
                    or wants_email_after_enrich
                    or _should_draft_after_prospect_search(user_msg or "", plan)
                ):
                    if directives.get("to") or directives.get("explicit_recipient_lock"):
                        for addr in directive_to_list(directives) or [directives.get("to")]:
                            if not addr:
                                continue
                            one = dict(directives)
                            one["to"] = addr
                            for chunk in _run_styled_directive_draft(
                                user_msg=user_msg or "",
                                directives=one,
                                enrichment=_lookup_enrichment_for(
                                    addr, results, prospect_out
                                ),
                                plan=plan,
                                attachments=email_atts,
                                draft_previews=draft_previews,
                            ):
                                yield chunk
                    else:
                        for chunk in _iter_draft_after_search(
                            user_msg=user_msg or "",
                            history=history,
                            context=context,
                            plan=plan,
                            prospects=results,
                        ):
                            yield chunk
                else:
                    yield (
                        "\nNext: `draft personalized emails to all these prospects` "
                        "— each draft is written for that person's organisation.\n"
                    )

            else:
                # Pull LinkedIn / company from the user message when JSON is sparse
                li = (
                    ident.get("linkedin_url")
                    or ident.get("linkedin")
                    or (batch_urls[0] if batch_urls else "")
                    or extract_linkedin_url(user_msg or "")
                )
                if li:
                    ident["linkedin_url"] = li
                    if not ident.get("first_name") or not ident.get("last_name"):
                        f, l = names_from_linkedin_url(li)
                        ident.setdefault("first_name", f)
                        ident.setdefault("last_name", l)
                if not ident.get("company"):
                    company_m = re.search(
                        r"\b(?:at|@|company|from)\s+([A-Za-z0-9&.\- ]{2,60})",
                        user_msg or "",
                        re.I,
                    )
                    if company_m:
                        ident["company"] = company_m.group(1).strip(" .,")
                # Domain like soprasteria.com
                if not ident.get("company_domain"):
                    dom_m = re.search(
                        r"\b(?:from|at|@)\s+([A-Za-z0-9][A-Za-z0-9.\-]*\.[A-Za-z]{2,})\b",
                        user_msg or "",
                        re.I,
                    )
                    if dom_m:
                        ident["company_domain"] = dom_m.group(1).lower().removeprefix("www.")
                if ident.get("company_domain") and not ident.get("company"):
                    label = str(ident["company_domain"]).split(".")[0].replace("-", " ")
                    ident["company"] = label.title()
                if not ident:
                    ident = {"name": user_msg}
    
                from core.prospect_list import (
                    find_by_person,
                    has_email,
                    wants_force_refresh,
                )
    
                result = None
                name_hint = (
                    ident.get("name")
                    or " ".join(
                        x
                        for x in [
                            ident.get("first_name") or "",
                            ident.get("last_name") or "",
                        ]
                        if x
                    )
                    or ""
                )
                company_hint = str(
                    ident.get("company") or ident.get("company_domain") or ""
                )
                li_url = str(ident.get("linkedin_url") or "")
                # LinkedIn URL → ZoomInfo (or session cache). Do not skip ZI
                # just because a similar name is already on the saved list.
                if li_url:
                    cached_li = get_cached_enrichment(li_url)
                    if cached_li:
                        result = {**cached_li, "from_cache": True}
                        yield "_Cached enrichment (no ZoomInfo credit used)._\n"
                    else:
                        yield "Enriching contact via ZoomInfo…\n"
                        result = _enrich_linkedin_cached(
                            li_url,
                            ident,
                            allow_multi=_allow_multi_provider(user_msg or ""),
                        )
                elif name_hint and not wants_force_refresh(user_msg or ""):
                    cached_people = find_by_person(
                        name_hint, company=company_hint, limit=5
                    )
                    if cached_people:
                        best = next((p for p in cached_people if has_email(p)), None)
                        if best is not None:
                            result = best
                            yield (
                                f"Found **{result.get('name') or 'contact'}** on your saved "
                                f"prospect list (email already on file).\n"
                            )
                        else:
                            yield (
                                f"Saved **{cached_people[0].get('name') or 'contact'}** "
                                "has no email — checking ZoomInfo…\n"
                            )
                    else:
                        yield (
                            f"Not on your saved list — searching **ZoomInfo** for "
                            f"**{name_hint}**"
                            + (f" @ {company_hint}" if company_hint else "")
                            + "…\n"
                        )
    
                if result is None:
                    if not name_hint or wants_force_refresh(user_msg or ""):
                        yield "Enriching contact via ZoomInfo…\n"
                    result = enrich_fallthrough(
                        ident,
                        linkedin_url=li_url or None,
                        allow_multi_provider=_allow_multi_provider(user_msg or ""),
                    )
                    if li_url and result and not result.get("error"):
                        put_cached_enrichment(li_url, result)
                if result and not result.get("error"):
                    prospect_out = [result]
                    try:
                        auto_ingest_prospects([result])
                    except Exception as e:
                        print(f"[router] enrich auto-ingest error: {e}", file=sys.stderr)
                    yield format_enrichment_panel(result) + "\n"
                elif result and result.get("error"):
                    yield f"ZoomInfo enrich failed: {result.get('error')}\n"
    
                # Same-turn draft after LinkedIn enrich (review before send)
                zi_email = str((result or {}).get("email") or "").strip()
                draft_to = (directives.get("to") or "").strip() or zi_email
                if (
                    wants_email_after_enrich
                    and result
                    and not result.get("error")
                    and draft_to
                ):
                    dirs = dict(directives)
                    dirs["to"] = draft_to
                    if dirs.get("cc") is None:
                        dirs["cc"] = list(plan.cc or [])
                    for chunk in _run_styled_directive_draft(
                        user_msg=user_msg or "",
                        directives=dirs,
                        enrichment=result,
                        plan=plan,
                        attachments=email_atts,
                        draft_previews=draft_previews,
                    ):
                        yield chunk
                    if email_atts:
                        consumed_attachments = True
                    if chat_attachments and not email_atts:
                        yield (
                            _attach_note(
                                chat_attachments,
                                used_document_context=used_docs,
                                attached_to_email=False,
                            )
                            + "\n"
                        )
                elif result and not (zi_email or draft_to):
                    yield "\n" + email_not_found_prompt() + "\n"
                else:
                    yield (
                        "\nSay `draft to name@company.com` to create a Gmail draft, "
                        "or paste another LinkedIn URL to enrich.\n"
                    )

        elif routing.startswith("GMAIL_EXTRACT"):
            raw_tail = ""
            if ":" in routing:
                raw_tail = routing.split(":", 1)[1].strip()
            gmail_q, tag = _normalize_gmail_query(user_msg, raw_tail)
            yield f"Reading Gmail (`{gmail_q}`)…\n"
            try:
                if tag == "both" or gmail_q.upper().startswith("BOTH"):
                    days_m = re.search(r"newer_than:(\d+)d", gmail_q)
                    days = int(days_m.group(1)) if days_m else 14
                    batch = extract_inbox_and_sent(
                        days=days,
                        max_per_mailbox=30,
                        ai_extract=False,
                        include_inbox=True,
                        include_sent=True,
                    )
                else:
                    batch = extract_batch(
                        gmail_q, max_results=40, ai_extract=False
                    )
                    for row in batch:
                        if not row.get("mailbox"):
                            row["mailbox"] = tag if tag != "custom" else "custom"
                filt_m = re.search(
                    r"\b(?:filter(?:ed)?(?:\s+by)?|matching|containing)\s+(.+)$",
                    user_msg or "",
                    re.I,
                )
                if filt_m and "subject:" not in gmail_q.lower():
                    batch = filter_messages(batch, filt_m.group(1))
            except Exception as e:
                yield f"Gmail read failed: {e}\n"
                batch = []

            mailbox_out = batch
            if not batch:
                yield (
                    "No messages found. Try e.g. "
                    "`show my inbox last 7 days` or `show sent about sponsor`."
                )
            else:
                yield f"Found **{len(batch)}** messages:\n\n"
                yield _format_mailbox_digest(batch)
                try:
                    counts = ingest_mailbox_messages(batch)
                    yield (
                        f"\n\nAuto-saved **{counts.get('emails', 0)}** emails + "
                        f"**{counts.get('contacts', 0)}** contacts to memory."
                    )
                except Exception as e:
                    print(f"[router] mailbox auto-ingest error: {e}", file=sys.stderr)
                yield (
                    "\n\nNext: ask me to **draft personalized follow-ups**, or clone one "
                    "by **id** / list number, e.g. "
                    "`create draft like message id <id> for Acme` or "
                    "`draft like #2 from sent for Flipkart`."
                )
            sources.append(
                {
                    "title": "gmail_extract",
                    "url": "",
                    "type": "gmail",
                    "count": len(batch),
                    "query": gmail_q,
                }
            )

        elif routing.startswith("DRAFT_EMAIL") or routing.startswith("SEND_EMAIL"):
            is_send = routing.startswith("SEND_EMAIL")
            prefix = "SEND_EMAIL:" if is_send else "DRAFT_EMAIL:"
            seed = (
                _parse_json_tail(routing, prefix)
                if routing.startswith(prefix)
                else {}
            )
            like_ref = str(
                plan.like_sent_to or seed.get("like_sent_to") or ""
            ).strip()
            like_for = str(
                plan.like_sent_for or seed.get("like_sent_for") or ""
            ).strip()
            like_mid = str(
                plan.like_sent_message_id
                or seed.get("like_sent_message_id")
                or ""
            ).strip()
            resolved_like = resolve_like_sent_from_history(
                user_msg or "",
                history,
                like_sent_to=like_ref,
                like_sent_for=like_for,
                like_sent_message_id=like_mid,
            )
            like_ref = (resolved_like.get("reference") or like_ref).strip()
            like_for = (resolved_like.get("target") or like_for).strip()
            like_mid = (resolved_like.get("message_id") or like_mid).strip()
            if not like_mid:
                like_mid = parse_gmail_message_id(user_msg or "")
            list_idx = parse_mailbox_list_index(user_msg or "")

            recipient_lock = bool(
                directives.get("explicit_recipient_lock")
                or directive_to_list(directives)
            )
            if recipient_lock:
                ignore_set = {e.lower() for e in (directives.get("ignore") or [])}
                locked = [
                    a for a in directive_to_list(directives) if a.lower() not in ignore_set
                ]
                n_session = len(_prospects_with_email(prospects))
                draft_debug["recipients_final"] = locked
                draft_debug["draft_path"] = "single"
                draft_debug["ignored_count"] = max(0, n_session - len(locked))
                dirs = dict(directives)
                if not dirs.get("cc"):
                    dirs["cc"] = list(plan.cc or [])
                for addr in locked:
                    one = dict(dirs)
                    one["to"] = addr
                    for chunk in _run_styled_directive_draft(
                        user_msg=user_msg or "",
                        directives=one,
                        enrichment=_lookup_enrichment_for(
                            addr, prospects, prospect_out
                        ),
                        plan=plan,
                        attachments=email_atts,
                        draft_previews=draft_previews,
                    ):
                        yield chunk
                if email_atts:
                    consumed_attachments = True

            # Resolve reference from mailbox list index (#2 / email 3)
            if not like_mid and list_idx and mailbox_messages:
                pool = [
                    m
                    for m in mailbox_messages
                    if (m.get("mailbox") or "").lower() == "sent"
                ] or list(mailbox_messages)
                if 1 <= list_idx <= len(pool):
                    picked = pool[list_idx - 1]
                    like_mid = str(picked.get("message_id") or "").strip()
                    if not like_ref:
                        # Best-effort company hint from To header
                        to_hdr = str(picked.get("to") or "")
                        dom = re.search(
                            r"@([A-Za-z0-9.\-]+)\.[A-Za-z]{2,}", to_hdr
                        )
                        if dom:
                            like_ref = dom.group(1).split(".")[0]

            if not recipient_lock and not directives.get("to") and (like_ref or like_mid):
                # Clone angle from a prior sent email → draft to named company /
                # last-search prospects — never the Sent template (Magic Bus / IndiaMART).
                explicit_company = (
                    parse_explicit_draft_company(user_msg or "")
                    or (like_for or "").strip()
                )
                use_prospect_batch = bool(
                    _prospects_with_email(prospects)
                    and (
                        bool(explicit_company)
                        or wants_prospect_list_recipients(user_msg or "")
                        or (
                            not plan.to_emails
                            and not wants_previous_chat_recipient(user_msg or "")
                        )
                    )
                )
                target_company = _infer_like_sent_target(
                    explicit=explicit_company or like_for,
                    reference=like_ref,
                    prospects=prospects,
                    history=history,
                    prefer_per_prospect=use_prospect_batch and not explicit_company,
                )
                if explicit_company and not target_company:
                    target_company = explicit_company
                yield (
                    "**Like-sent:** "
                    + (
                        f"loading message id `{like_mid}`"
                        if like_mid
                        else f"finding Gmail sent to **{like_ref or 'prior email'}**"
                    )
                    + (
                        f", adapting for **{target_company}**"
                        if target_company
                        else (
                            ", personalizing per prospect company"
                            if use_prospect_batch
                            else ""
                        )
                    )
                    + "…\n"
                )
                if _stop_now():
                    yield stopped_message()
                else:
                    sent_rows: list[dict[str, Any]] = []
                    ref_msg: Optional[dict[str, Any]] = None

                    # 1) Direct fetch by Gmail message id (preferred)
                    if like_mid:
                        try:
                            fresh = get_message(like_mid)
                            if fresh.get("error"):
                                yield (
                                    f"_Couldn't load message id `{like_mid}`: "
                                    f"{fresh.get('error')}_\n"
                                )
                            else:
                                ref_msg = {
                                    **fresh,
                                    "message_id": like_mid,
                                    "mailbox": "sent",
                                    "extracted": {
                                        "summary": (fresh.get("body_text") or "")[
                                            :500
                                        ],
                                    },
                                }
                                sent_rows = [ref_msg]
                                if not like_ref:
                                    to_hdr = str(fresh.get("to") or "")
                                    dom = re.search(
                                        r"@([A-Za-z0-9.\-]+)\.[A-Za-z]{2,}",
                                        to_hdr,
                                    )
                                    if dom:
                                        like_ref = dom.group(1).split(".")[0]
                                    elif fresh.get("subject"):
                                        like_ref = "prior sent email"
                                yield (
                                    f"_Loaded id=`{like_mid}` · "
                                    f"**{(fresh.get('subject') or '(no subject)').strip()}** "
                                    f"→ {fresh.get('to') or '—'}_\n"
                                )
                        except Exception as e:
                            print(
                                f"[router] like-sent by id: {e}", file=sys.stderr
                            )
                            yield f"_Couldn't load message id `{like_mid}`: {e}_\n"

                    # 2) Company search in Sent
                    if not ref_msg and like_ref:
                        try:
                            sent_rows = find_sent_to_company(
                                like_ref,
                                days=365,
                                max_results=15,
                                ai_extract=False,
                            )
                        except Exception as e:
                            print(f"[router] like-sent gmail: {e}", file=sys.stderr)
                            yield f"_Couldn't search Sent mail: {e}_\n"
                        # Fallback: filter already-loaded mailbox (prefer sent)
                        if not sent_rows and mailbox_messages:
                            sent_only = [
                                m
                                for m in mailbox_messages
                                if (m.get("mailbox") or "").lower() == "sent"
                            ]
                            pool = sent_only or list(mailbox_messages)
                            sent_rows = filter_messages(pool, like_ref)
                        ref_msg = pick_best_sent_reference(sent_rows, like_ref)

                    mailbox_out = list(sent_rows)
                    if ref_msg and ref_msg.get("message_id"):
                        # Always re-fetch full MIME so we don't draft from a stub
                        try:
                            fresh = get_message(str(ref_msg["message_id"]))
                            if not fresh.get("error") and (
                                fresh.get("body_text") or fresh.get("body_html")
                            ):
                                ref_msg = {
                                    **ref_msg,
                                    **{
                                        k: fresh.get(k) or ref_msg.get(k) or ""
                                        for k in (
                                            "subject",
                                            "from",
                                            "to",
                                            "cc",
                                            "date",
                                            "body_text",
                                            "body_html",
                                            "thread_id",
                                        )
                                    },
                                    "mailbox": "sent",
                                }
                        except Exception as e:
                            print(
                                f"[router] like-sent refresh: {e}",
                                file=sys.stderr,
                            )
                    if not ref_msg:
                        yield (
                            "I couldn't find that sent email"
                            + (f" (**{like_ref}**)" if like_ref else "")
                            + (f" / id `{like_mid}`" if like_mid else "")
                            + ". Try `show sent last 365 days`, then "
                            "`create draft like message id <id> for <company>` "
                            "or `draft like #1 from sent`.\n"
                        )
                    elif not (
                        (ref_msg.get("body_text") or "").strip()
                        or (ref_msg.get("body_html") or "").strip()
                    ):
                        yield (
                            f"Found a sent message for **{like_ref}** "
                            f"(**{(ref_msg.get('subject') or '(no subject)').strip()}**) "
                            "but the body came back empty. "
                            "Open it in Gmail Sent and try again, or paste the body here.\n"
                        )
                    else:
                        # Prefer a full email pasted in chat over a truncated Gmail body
                        pasted = _extract_pasted_email(user_msg or "")
                        gmail_body = (
                            (ref_msg.get("body_text") or "").strip()
                            or (ref_msg.get("body_html") or "").strip()
                        )
                        if pasted and len(pasted) > len(gmail_body) + 100:
                            ref_msg = {
                                **ref_msg,
                                "body_text": pasted,
                                "body_html": "",
                            }
                            yield (
                                "_Using the **full email you pasted** as the reference "
                                f"({len(pasted)} chars) — fuller than the Gmail capture._\n"
                            )
                        ref_subj = (ref_msg.get("subject") or "(no subject)").strip()
                        body_preview = (ref_msg.get("body_text") or "").strip()
                        if len(body_preview) < 80 and ref_msg.get("body_html"):
                            try:
                                from bs4 import BeautifulSoup

                                body_preview = (
                                    BeautifulSoup(
                                        ref_msg.get("body_html") or "",
                                        "html.parser",
                                    )
                                    .get_text("\n")
                                    .strip()
                                )
                            except Exception:
                                pass
                        preview = body_preview[:700].replace("\n", " ").strip()
                        if len(body_preview) > 700:
                            preview += "…"
                        yield (
                            f"Using reference: **{ref_subj}** "
                            f"(to {ref_msg.get('to') or '—'}; "
                            f"{ref_msg.get('date') or ''})\n"
                            f"_Captured body: **{len(body_preview)}** chars"
                            + (
                                f" + HTML **{len(ref_msg.get('body_html') or '')}**"
                                if ref_msg.get("body_html")
                                else ""
                            )
                            + f"_\n> {preview}\n"
                        )
                        if len(body_preview) < 1500:
                            yield (
                                "_Note: Sent capture looks short for a full proposal. "
                                "Paste the complete email in chat if sections are missing._\n"
                            )
                        research_notes = ""
                        composed_by_company: dict[str, dict[str, str]] = {}
                        ref_org = _reference_org_for_swap(like_ref, ref_msg)
                        scrub_names = _reference_org_aliases(like_ref, ref_msg, ref_org)
                        if "@" in (like_ref or ""):
                            yield (
                                f"_Template recipient: `{like_ref}` · "
                                f"scrub labels: **{', '.join(scrub_names[:4])}**_\n"
                            )

                        # Resolve prospect recipients early so we know which
                        # companies need Gemini research (not Magic Bus).
                        block = set(plan.non_recipient_emails())
                        if like_ref and "@" in like_ref:
                            block.add(like_ref.lower())
                            try:
                                block.add(like_ref.split("@", 1)[1].lower())
                            except Exception:
                                pass

                        early_matched: list[dict[str, Any]] = []
                        if use_prospect_batch or target_company:
                            early_matched = list(_prospects_with_email(prospects))
                            # If chat lost last_prospects, pull saved Sterlite/etc. contacts
                            if target_company and not early_matched:
                                try:
                                    from core.prospect_list import find_by_company

                                    early_matched = [
                                        p
                                        for p in find_by_company(
                                            target_company, limit=50, require_email=True
                                        )
                                        if (p.get("email") or "").strip()
                                    ]
                                except Exception as e:
                                    print(
                                        f"[router] prospect_list fallback: {e}",
                                        file=sys.stderr,
                                    )
                            if target_company and early_matched:
                                filtered_co = _prospects_for_company(
                                    early_matched, target_company
                                )
                                if filtered_co:
                                    early_matched = filtered_co
                            filtered_early: list[dict[str, Any]] = []
                            for p in early_matched:
                                em = (p.get("email") or "").strip()
                                if not em or em.lower() in block:
                                    continue
                                if like_ref and "@" in like_ref:
                                    if em.lower() == like_ref.lower():
                                        continue
                                # Never draft back to common template orgs from prior runs
                                em_l = em.lower()
                                if any(
                                    em_l.endswith(d)
                                    for d in (
                                        "@magicbusindia.org",
                                        "@indiamart.com",
                                        "@indiamart.co.in",
                                    )
                                ):
                                    continue
                                filtered_early.append(p)
                            early_matched = filtered_early
                            if early_matched:
                                use_prospect_batch = True

                        companies_to_research: list[str] = []
                        if target_company and target_company not in (
                            "{company}",
                            like_ref,
                            org_label_from_email(like_ref)
                            if "@" in (like_ref or "")
                            else "",
                        ):
                            companies_to_research = [target_company]
                        elif early_matched:
                            companies_to_research = _unique_prospect_companies(
                                early_matched, limit=8
                            )
                        elif like_for:
                            companies_to_research = [like_for]

                        ref_body_for_research = (
                            (ref_msg.get("body_text") or body_preview) or ""
                        )[:12000]
                        for company in companies_to_research:
                            if _stop_now():
                                yield stopped_message()
                                break
                            yield (
                                f"**Gemini research** for **{company}** "
                                "(grounded web search)…\n"
                            )
                            notes, web_sources = _research_company_for_like_sent(
                                company=company,
                                ref_subj=ref_subj,
                                ref_to=str(ref_msg.get("to") or ""),
                                ref_body=ref_body_for_research,
                                user_msg=user_msg or "",
                            )
                            if web_sources:
                                sources.extend(web_sources)
                            if not notes:
                                yield f"_Research limited for **{company}** — still personalizing names._\n"
                            composed_by_company[company.lower()] = (
                                _compose_like_sent_for_company(
                                    user_msg=user_msg or "",
                                    reference_msg=ref_msg,
                                    reference_company=ref_org,
                                    scrub_names=scrub_names,
                                    target_company=company,
                                    research_notes=notes,
                                    document_context=doc_context,
                                )
                            )
                            research_notes = notes or research_notes
                            if notes:
                                align = (
                                    composed_by_company[company.lower()].get(
                                        "alignment_summary"
                                    )
                                    or ""
                                )
                                if align:
                                    yield f"_{company}: {align}_\n"

                        # Fallback template with {company} when no research targets
                        compose_target = target_company or (
                            "{company}" if use_prospect_batch or early_matched else ""
                        )
                        if composed_by_company:
                            # Prefer explicit target, else first researched company
                            pick = (target_company or companies_to_research[0]).lower()
                            composed = composed_by_company.get(pick) or next(
                                iter(composed_by_company.values())
                            )
                        else:
                            if use_prospect_batch:
                                yield (
                                    "_No company names on the prospect list for Gemini "
                                    "research — using name-swap personalization only._\n"
                                )
                            composed = _compose_like_sent_for_company(
                                user_msg=user_msg or "",
                                reference_msg=ref_msg,
                                reference_company=ref_org,
                                scrub_names=scrub_names,
                                target_company=compose_target or "{company}",
                                research_notes="",
                                document_context=doc_context,
                            )
                        yield (
                            f"_Draft keeps **{composed.get('paragraph_count') or '?'}** "
                            f"sections · **{composed.get('char_count') or '?'}** chars "
                            f"(Gemini-personalized per company; scrubbed template org)._\n"
                        )
                        if int(composed.get("char_count") or 0) < 1500:
                            yield (
                                "_Warning: the captured Sent body looks short. "
                                "If sections are missing, paste the full email in chat "
                                "and ask again (e.g. create email like this for Flipkart)._\n"
                            )

                        payload = {
                            "subject": composed.get("subject") or ref_subj,
                            "html_body": composed.get("html_body") or "",
                            "source": "like_sent",
                            "campaign": f"like:{like_ref}",
                        }
                        if email_atts:
                            payload["attachments"] = email_atts
                        if plan.cc:
                            payload["cc"] = plan.cc
                        if plan.ignore_emails:
                            payload["ignore_emails"] = plan.ignore_emails

                        recipients: list[str] = [
                            e
                            for e in (plan.to_emails or [])
                            if e
                            and e.lower() not in block
                            and not (
                                like_ref
                                and "@" in like_ref
                                and e.lower().endswith(
                                    "@" + like_ref.split("@", 1)[1].lower()
                                )
                            )
                        ]
                        # "to above" / last search → always use prospect list, not
                        # history To addresses and never the Magic Bus template.
                        if use_prospect_batch:
                            recipients = []
                            matched = early_matched
                            if matched:
                                payload["batch"] = True
                                payload["from_prospects"] = True
                                prospects = matched
                                yield (
                                    f"_To (**{target_company or 'searched company'}** "
                                    f"contacts): **{len(matched)}** — each draft is "
                                    f"**Gemini-researched** for that company "
                                    f"(not the Sent template)_\n"
                                )
                            else:
                                yield (
                                    f"_No emailed contacts found for "
                                    f"**{target_company or 'that company'}**. "
                                    f"Search contacts for that org first, then ask again._\n"
                                )
                        elif not recipients or wants_previous_chat_recipient(
                            user_msg or ""
                        ):
                            # Never use history when an explicit company was named
                            if target_company or parse_explicit_draft_company(
                                user_msg or ""
                            ):
                                yield (
                                    f"_Need Sterlite/company contacts on the prospect "
                                    f"list for **{target_company or 'the named company'}** "
                                    f"— not prior Magic Bus drafts from chat._\n"
                                )
                            else:
                                hist_tos = resolve_to_emails_from_history(
                                    history, exclude=block
                                )
                                # Prefer history when user asked for previous/chat recipient
                                if wants_previous_chat_recipient(
                                    user_msg or ""
                                ) and hist_tos:
                                    recipients = hist_tos
                                elif not recipients:
                                    recipients = hist_tos

                        if not use_prospect_batch and recipients:
                            if len(recipients) == 1:
                                payload["recipient_email"] = recipients[0]
                            else:
                                payload["recipient_emails"] = recipients
                                payload["batch"] = True
                            yield (
                                f"_To (from chat / request): "
                                f"{', '.join(recipients[:5])}"
                                + (
                                    f" +{len(recipients) - 5} more"
                                    if len(recipients) > 5
                                    else ""
                                )
                                + "_\n"
                            )
                        elif not use_prospect_batch and prospects:
                            matched = _prospects_for_company(
                                prospects, target_company
                            )
                            # Drop any prospect that is the Sent template address/domain
                            filtered_m = []
                            for p in matched:
                                em = (p.get("email") or "").strip()
                                if not em or em.lower() in block:
                                    continue
                                if like_ref and "@" in like_ref:
                                    if em.lower() == like_ref.lower():
                                        continue
                                filtered_m.append(p)
                            matched = filtered_m
                            if matched:
                                payload["batch"] = True
                                payload["from_prospects"] = True
                                prospects = matched
                                # Research any companies not covered yet
                                for company in _unique_prospect_companies(matched):
                                    if company.lower() in composed_by_company:
                                        continue
                                    yield (
                                        f"**Gemini research** for **{company}**…\n"
                                    )
                                    notes, web_sources = _research_company_for_like_sent(
                                        company=company,
                                        ref_subj=ref_subj,
                                        ref_to=str(ref_msg.get("to") or ""),
                                        ref_body=ref_body_for_research,
                                        user_msg=user_msg or "",
                                    )
                                    sources.extend(web_sources)
                                    composed_by_company[company.lower()] = (
                                        _compose_like_sent_for_company(
                                            user_msg=user_msg or "",
                                            reference_msg=ref_msg,
                                            reference_company=ref_org,
                                            scrub_names=scrub_names,
                                            target_company=company,
                                            research_notes=notes,
                                            document_context=doc_context,
                                        )
                                    )
                            else:
                                yield (
                                    "_No matching prospects for the **current** org "
                                    "(skipped Magic Bus / template domain). "
                                    "Give a To address from chat, e.g. "
                                    "`draft to person@currentorg.org`._\n"
                                )

                        # Sanitize user_msg so _build_draft_jobs cannot re-scrape
                        # the template email as To via "email like info@…"
                        draft_msg = user_msg or ""
                        if like_ref and "@" in like_ref:
                            draft_msg = re.sub(
                                re.escape(like_ref),
                                "[sent-template]",
                                draft_msg,
                                flags=re.I,
                            )

                        jobs = _build_draft_jobs(
                            payload,
                            draft_msg,
                            history=history,
                            prospects=prospects,
                            mailbox_messages=None,  # never follow-up path
                            plan=plan,
                        )
                        # Hard filter: drop any job still addressed to the template
                        safe_jobs = []
                        for job in jobs:
                            em = (job.get("recipient_email") or "").strip().lower()
                            if not em or em in block:
                                continue
                            if like_ref and "@" in like_ref:
                                if em == like_ref.lower():
                                    continue
                            safe_jobs.append(job)
                        if jobs and not safe_jobs:
                            yield (
                                "_Blocked draft(s) to the Sent **template** address "
                                f"(`{like_ref}`). Using chat history for To instead…_\n"
                            )
                            hist_tos = resolve_to_emails_from_history(
                                history, exclude=block
                            )
                            if hist_tos:
                                payload.pop("from_prospects", None)
                                payload.pop("use_prospects", None)
                                if len(hist_tos) == 1:
                                    payload["recipient_email"] = hist_tos[0]
                                    payload.pop("recipient_emails", None)
                                else:
                                    payload["recipient_emails"] = hist_tos
                                    payload["batch"] = True
                                jobs = _build_draft_jobs(
                                    payload,
                                    draft_msg,
                                    history=history,
                                    prospects=None,
                                    mailbox_messages=None,
                                    plan=plan,
                                )
                                safe_jobs = [
                                    j
                                    for j in jobs
                                    if (j.get("recipient_email") or "").lower()
                                    not in block
                                ]
                        jobs = safe_jobs
                        # Per-recipient: Gemini-composed body for that company + name fill
                        if jobs:
                            by_email = {
                                (p.get("email") or "").strip().lower(): p
                                for p in (prospects or [])
                                if (p.get("email") or "").strip()
                            }
                            for job in jobs:
                                em = (job.get("recipient_email") or "").strip().lower()
                                pctx = by_email.get(em) or {
                                    "name": job.get("recipient_name") or "",
                                    "first_name": (
                                        str(job.get("first_name") or "").strip()
                                        or str(job.get("recipient_name") or "")
                                        .split(None, 1)[0]
                                    ),
                                    "email": job.get("recipient_email") or "",
                                    "recipient_email": job.get("recipient_email") or "",
                                    "title": job.get("title")
                                    or job.get("designation")
                                    or "",
                                    "company": target_company
                                    or job.get("company")
                                    or "",
                                }
                                company = (
                                    str(pctx.get("company") or "").strip()
                                    or target_company
                                    or ""
                                )
                                if company and not (pctx.get("company") or "").strip():
                                    pctx = {**pctx, "company": company}
                                company_composed = (
                                    composed_by_company.get(company.lower())
                                    if company
                                    else None
                                ) or composed
                                personalized = _personalize_like_sent_job(
                                    subject=company_composed.get("subject")
                                    or job.get("subject")
                                    or "",
                                    html_body=company_composed.get("html_body")
                                    or job.get("html_body")
                                    or "",
                                    prospect=pctx,
                                    scrub_names=scrub_names
                                    + (
                                        ["{company}"]
                                        if "{company}"
                                        in (
                                            (company_composed.get("subject") or "")
                                            + (company_composed.get("html_body") or "")
                                        )
                                        else []
                                    ),
                                )
                                job["subject"] = personalized["subject"]
                                job["html_body"] = personalized["html_body"]
                                job["company"] = company

                        email_cap = min(
                            max(int(plan.email_limit or MAX_EMAILS), 1), MAX_EMAILS
                        )
                        if len(jobs) > email_cap:
                            yield (
                                f"_Capping at **{email_cap}** emails "
                                f"(found {len(jobs)})._\n"
                            )
                            jobs = apply_email_cap(jobs, email_limit=email_cap)

                        headers = _mail_headers(
                            user_msg,
                            seed=payload,
                            to_emails=[j.get("recipient_email") or "" for j in jobs],
                            plan=plan,
                        )
                        payload["from_email"] = headers["from_email"]
                        payload["cc"] = headers["cc"]
                        want_send = is_send and not _prefer_draft_over_send(
                            user_msg, True
                        )
                        action = "send" if want_send else "draft"

                        if not jobs:
                            # Still save the composed email to Drafts for review
                            # (common when prospect list was empty after a redeploy).
                            saved = False
                            try:
                                from gmail_client.send import save_drive_only_draft

                                body = payload.get("html_body") or ""
                                subj = payload.get("subject") or "(no subject)"
                                if body.strip():
                                    fb = save_drive_only_draft(
                                        to="",
                                        subject=subj,
                                        html_body=body,
                                        company=target_company or "",
                                        from_email=headers.get("from_email"),
                                        cc=headers.get("cc"),
                                        source="like_sent_needs_to",
                                        gmail_error="missing recipient",
                                    )
                                    if fb.get("draft_id"):
                                        saved = True
                                        yield (
                                            f"I wrote a **{like_ref}**-style email for "
                                            f"**{target_company or 'your current org'}**, "
                                            f"but need a **To** address. Saved to "
                                            f"**Drafts** (`{fb.get('draft_id')}`) — "
                                            f"open it, set To, then send.\n\n"
                                            f"Or say e.g. `draft to person@currentorg.org` "
                                            f"or search contacts for that company first.\n"
                                        )
                            except Exception as e:
                                print(
                                    f"[router] like_sent drive fallback: {e}",
                                    file=sys.stderr,
                                )
                            if not saved:
                                yield (
                                    f"I wrote a **{like_ref}**-style email for "
                                    f"**{target_company or 'your current org'}**, but need the "
                                    "**To** address from your chat (not the Sent template). "
                                    "Say e.g. `draft to person@currentorg.org` or "
                                    "`use the previous email from chat`.\n\n"
                                    f"**Subject:** {payload.get('subject')}\n\n"
                                    f"{payload.get('html_body')}\n"
                                )
                        else:
                            yield (
                                f"{'Sending' if want_send else 'Creating Gmail draft(s)'} "
                                f"from **{headers['from_email']}**"
                                + (
                                    f" (cc: {', '.join(headers['cc'])})"
                                    if headers["cc"]
                                    else ""
                                )
                                + f" · style of sent-to-**{like_ref}**…\n"
                            )
                            results = []
                            for job in jobs:
                                if _stop_now():
                                    yield stopped_message()
                                    break
                                # Ensure company placeholder context
                                if target_company and "{company}" not in (
                                    job.get("subject") or ""
                                ):
                                    pass
                                job = _stamp_mail_fields(
                                    job,
                                    from_email=headers["from_email"],
                                    cc=headers["cc"],
                                    attachments=email_atts,
                                )
                                job["source"] = "like_sent"
                                out, did_send = _deliver_job(
                                    job, want_send=want_send, user_msg=user_msg
                                )
                                if not out.get("error") and not did_send:
                                    yield _record_draft_preview(out, draft_previews)
                                results.append({**out, "_did_send": did_send})
                            ok = [r for r in results if not r.get("error")]
                            fail = [r for r in results if r.get("error")]
                            if ok and email_atts:
                                consumed_attachments = True
                            yield f"Done: **{len(ok)}** ok"
                            if fail:
                                yield f", **{len(fail)}** failed"
                            yield (
                                _attach_note(
                                    chat_attachments
                                    if chat_attachments
                                    else email_atts,
                                    used_document_context=used_docs,
                                    attached_to_email=bool(email_atts),
                                )
                                + "\n"
                            )
                            for r in ok[:100]:
                                target = r.get("to") or r.get("recipient_email")
                                did = r.get("_did_send")
                                yield (
                                    f"- {'Sent' if did else 'Draft'} → {target}"
                                    + (f" cc {r.get('cc')}" if r.get("cc") else "")
                                    + (
                                        f" (draft_id={r.get('draft_id')})"
                                        if r.get("draft_id")
                                        else ""
                                    )
                                    + "\n"
                                )
                            if not want_send and ok:
                                yield (
                                    "\nOpen **Drafts** (or Gmail → Drafts) to review, then send. "
                                    "Tracking is embedded for 📬 Tracking after send.\n"
                                )
                            for r in fail[:10]:
                                yield f"- Failed: {r.get('error')}\n"
                            try:
                                ingest_mailbox_messages(mailbox_out)
                            except Exception:
                                pass

            elif not recipient_lock and not directives.get("to"):
                session_n = len(_prospects_with_email(prospects))
                _ask = (
                    not looks_like_bulk_request(user_msg or "")
                    and not wants_prospect_list_recipients(user_msg or "")
                    and not re.search(
                        r"\b(follow[- ]?up|from (this|the|my) (list|inbox|sent|mailbox)|"
                        r"everyone (here|in (the|this) list))\b",
                        user_msg or "",
                        re.I,
                    )
                    and session_n > 1
                    and not (plan.to_emails or [])
                )
                if _ask:
                    draft_debug["draft_path"] = "ask"
                    draft_debug["recipients_final"] = []
                    draft_debug["ignored_count"] = session_n
                    yield _ask_who_to_draft(prospects)
                else:
                    # Prefer mailbox follow-ups when user asks and we have a prior pull
                    if mailbox_messages and re.search(
                        r"\b(follow[- ]?up|from (this|the|my) (list|inbox|sent|mailbox)|everyone (here|in (the|this) list))\b",
                        user_msg or "",
                        re.I,
                    ):
                        seed = {**seed, "batch": True, "from_mailbox": True}
                    # Bulk to last ZoomInfo / prospect search ("to above", etc.)
                    if prospects and wants_prospect_list_recipients(user_msg or ""):
                        seed = {**seed, "batch": True, "from_prospects": True}
                    payload = _extract_email_job(
                        user_msg,
                        history=history,
                        seed=seed,
                        for_schedule=False,
                        document_context=doc_context,
                    )
                    for flag in (
                        "batch",
                        "from_prospects",
                        "use_prospects",
                        "from_mailbox",
                        "use_mailbox",
                        "follow_up",
                        "mailbox_filter",
                        "recipient_emails",
                    ):
                        if flag in seed and flag not in payload:
                            payload[flag] = seed[flag]
                    # Attach binary files only when explicitly requested
                    if email_atts:
                        payload["attachments"] = email_atts
                    elif "attachments" in payload:
                        payload.pop("attachments", None)
                    # Default personalized follow-up templates when missing
                    if payload.get("from_mailbox") or payload.get("use_mailbox"):
                        if not payload.get("subject") or payload.get("subject") in (
                            "(no subject)",
                            user_msg,
                        ):
                            payload["subject"] = "Following up: {prior_subject}"
                        body = payload.get("html_body") or ""
                        if (
                            not body
                            or body.strip() == f"<p>{user_msg}</p>"
                            or "{first_name}" not in body
                        ):
                            payload["html_body"] = (
                                "<p>Hi {first_name},</p>"
                                "<p>I wanted to follow up on <strong>{prior_subject}</strong>.</p>"
                                "<p>{prior_summary}</p>"
                                "<p>Would you have time this week for a quick chat?</p>"
                                "<p>Best regards</p>"
                            )

                    if plan.cc and not payload.get("cc"):
                        payload["cc"] = plan.cc
                    if plan.ignore_emails:
                        payload["ignore_emails"] = plan.ignore_emails
                    if plan.to_emails and not payload.get("recipient_email") and not payload.get(
                        "recipient_emails"
                    ):
                        if len(plan.to_emails) == 1:
                            payload["recipient_email"] = plan.to_emails[0]
                        else:
                            payload["recipient_emails"] = plan.to_emails
                            payload["batch"] = True

                    jobs = _build_draft_jobs(
                        payload,
                        user_msg,
                        history=history,
                        prospects=prospects,
                        mailbox_messages=mailbox_messages,
                        plan=plan,
                    )
                    draft_debug["draft_path"] = (
                        "bulk" if len(jobs) > 1 else "single"
                    )
                    draft_debug["recipients_final"] = [
                        j.get("recipient_email") or "" for j in jobs
                    ]
                    draft_debug["ignored_count"] = max(
                        0,
                        session_n - len(draft_debug["recipients_final"]),
                    )
                    email_cap = min(max(int(plan.email_limit or MAX_EMAILS), 1), MAX_EMAILS)
                    if len(jobs) > email_cap:
                        yield (
                            f"_Capping at **{email_cap}** emails "
                            f"(found {len(jobs)}; ask for up to 100 if you need more)._\n"
                        )
                        jobs = apply_email_cap(jobs, email_limit=email_cap)
                    headers = _mail_headers(
                        user_msg,
                        seed=payload,
                        to_emails=[j.get("recipient_email") or "" for j in jobs],
                        plan=plan,
                    )
                    payload["from_email"] = headers["from_email"]
                    payload["cc"] = headers["cc"]
                    want_send = is_send and not _prefer_draft_over_send(user_msg, True)
                    action = "send" if want_send else "draft"
                    if not jobs and (
                        payload.get("from_mailbox") or payload.get("use_mailbox")
                    ):
                        yield (
                            f"I couldn't {action} follow-ups — no mailbox contacts loaded yet. "
                            "First say `show my inbox` or `show sent last 30 days`, "
                            "optionally filter, then ask for personalized follow-ups."
                        )
                    elif not jobs:
                        yield (
                            f"I couldn't {action} — no recipient emails found. "
                            "List addresses, search prospects, or pull inbox/sent first."
                        )
                    else:
                        yield (
                            f"{'Sending' if want_send else 'Creating new Gmail draft(s)'} "
                            f"from **{headers['from_email']}**"
                            + (
                                f" (cc: {', '.join(headers['cc'])})"
                                if headers["cc"]
                                else ""
                            )
                            + " with your signature…\n"
                        )
                        results = []
                        for job in jobs:
                            if _stop_now():
                                yield stopped_message()
                                break
                            job = _stamp_mail_fields(
                                job,
                                from_email=headers["from_email"],
                                cc=headers["cc"],
                                attachments=email_atts,
                            )
                            out, did_send = _deliver_job(
                                job, want_send=want_send, user_msg=user_msg
                            )
                            if not out.get("error") and not did_send:
                                yield _record_draft_preview(out, draft_previews)
                            results.append({**out, "_did_send": did_send})
                        ok = [r for r in results if not r.get("error")]
                        fail = [r for r in results if r.get("error")]
                        if ok and email_atts:
                            consumed_attachments = True
                        yield f"Done: **{len(ok)}** ok"
                        if fail:
                            yield f", **{len(fail)}** failed"
                        yield (
                            _attach_note(
                                chat_attachments if chat_attachments else email_atts,
                                used_document_context=used_docs,
                                attached_to_email=bool(email_atts),
                            )
                            + "\n"
                        )
                        for r in ok[:100]:
                            target = r.get("to") or r.get("recipient_email")
                            did = r.get("_did_send")
                            yield (
                                f"- {'Sent' if did else 'Draft'} → {target}"
                                + (f" cc {r.get('cc')}" if r.get("cc") else "")
                                + (
                                    f" (draft_id={r.get('draft_id')})"
                                    if r.get("draft_id")
                                    else ""
                                )
                                + "\n"
                            )
                        if len(ok) > 100:
                            yield f"_…and {len(ok) - 100} more._\n"
                        if not want_send:
                            yield (
                                "\nOpen **Drafts** (or Gmail → Drafts) to review, then send. "
                                "Open/click tracking is already embedded "
                                "(visible in 📬 Tracking after send).\n"
                            )
                        for r in fail[:10]:
                            yield f"- Failed: {r.get('error')}\n"

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
            job = _resolve_recipient(job, user_msg, history, plan=plan)
            if plan.cc:
                job["cc"] = plan.cc
            if plan.ignore_emails:
                job["ignore_emails"] = plan.ignore_emails
            job["from_email"] = plan.from_email or default_from_email()

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
                    attachments=email_atts or None,
                )
                yield f"Scheduled email to {job['recipient_email']} at {send_at}."
                yield (
                    _attach_note(
                        chat_attachments if chat_attachments else email_atts,
                        used_document_context=used_docs,
                        attached_to_email=bool(email_atts),
                    )
                    + "\n"
                )
                yield f"Result: {json.dumps(result, default=str)}"
                if email_atts:
                    consumed_attachments = True

        else:
            # CHAT (default) — ground in chat/session memory; avoid inventing facts
            mem_hits = None
            try:
                if prefers_chat_over_search(user_msg) or re.search(
                    r"\b(remember|recall|what did|who did|which email)\b",
                    user_msg or "",
                    re.I,
                ):
                    mem_hits = mem.search(user_msg, k=6)
            except Exception as e:
                print(f"[router] memory search: {e}", file=sys.stderr)
            system = chat_grounding_system(
                history=history,
                prospects=prospects,
                mailbox_messages=mailbox_messages,
                memory_hits=mem_hits,
                document_context=doc_context if used_docs else "",
                attachment_names=att_names or None,
            )
            # Prefer chat facts over web when user refers to prior conversation
            use_search = not prefers_chat_over_search(user_msg)
            for chunk in chat_grounded(
                user_msg,
                history=history,
                system=system,
                use_search=use_search,
            ):
                if isinstance(chunk, dict) and "__meta__" in chunk:
                    sources = chunk["__meta__"].get("sources") or []
                else:
                    yield chunk

    except Exception as e:
        print(f"[router] answer error: {e}", file=sys.stderr)
        yield f"[error] {e}"

    yield {
        "__meta__": {
            "routing": "STOPPED" if cancelled else meta_routing,
            "sources": sources,
            "consumed_attachments": consumed_attachments,
            "need_file": need_file,
            "mailbox_messages": mailbox_out or None,
            "prospects": prospect_out or None,
            "draft_previews": draft_previews or None,
            "draft_debug": draft_debug,
            "cancelled": cancelled,
            "pending_user_msg": user_msg if need_file else None,
        }
    }
