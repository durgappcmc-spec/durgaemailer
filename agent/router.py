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
from connectors.zoominfo import extract_linkedin_url, names_from_linkedin_url
from core.auto_sync import auto_ingest_prospects, ingest_mailbox_messages
from core import memory as mem
from core.llm import chat_fast, chat_grounded, extract_json
from agent.research_pipeline import (
    run_research_then_zoom,
    wants_research_then_zoom,
)
from gmail_client.attachments import document_context_from_attachments
from gmail_client.extract import (
    contacts_from_mailbox,
    extract_batch,
    extract_inbox_and_sent,
    filter_messages,
)
from gmail_client.send import (
    create_draft,
    default_cc_emails,
    default_from_email,
    send_email,
)
from scheduling.client import schedule_email

ROUTER_SYSTEM = """You are a routing classifier for Relay, a prospect research and outreach tool.
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
- RESEARCH_THEN_ZOOM: use when the user asks for orgs matching a MISSION / demographic
  (e.g. NGOs for girls 16+ skilling, women livelihoods) and then wants ZoomInfo contacts
  and/or emails / drafts. Do NOT dump demographics into ZoomInfo filters.
  Example: RESEARCH_THEN_ZOOM:{"org_limit":8,"contacts_per_org":3,"draft":true}
  Set draft:true if they also ask to draft/write personalized emails in the same message.
  Set send:true only if they explicitly say send now.
- PROSPECT_SEARCH: find people when the user already knows company/title filters.
  JSON keys may include titles, company_names, company_domains, locations, seniorities, keywords, providers (array), limit.
  Prefer ZoomInfo when the user says ZoomInfo / ZI. Example:
  PROSPECT_SEARCH:{"titles":["CEO"],"company_names":["Acme"],"providers":["zoominfo"],"limit":20}
  For plain NGO staff lookups without mission filters: company_names=["NGO"], locations=["Noida"].
  Never ask the user for ZoomInfo credentials.
- After a prospect / research search, if the user asks to email/draft/send to that list,
  use DRAFT_EMAIL or SEND_EMAIL with {"batch":true,"from_prospects":true,"subject":"..."}.
- PROSPECT_ENRICH: enrich one person. JSON keys: first_name, last_name, email, company, linkedin_url, title.
  When the user pastes a LinkedIn profile URL, ALWAYS use PROSPECT_ENRICH with linkedin_url set.
  Example: PROSPECT_ENRICH:{"linkedin_url":"https://www.linkedin.com/in/jane-doe","company":"Acme"}
  If they also ask to draft/send an email to that person in the same message, still choose PROSPECT_ENRICH
  (the app will enrich then draft/send). Never ask the user for ZoomInfo credentials — they are already configured.
- GMAIL_EXTRACT: read / list / filter the user's Gmail inbox and/or sent mail.
  After the colon put a Gmail search query (NOT prose).
  Examples:
    GMAIL_EXTRACT:in:inbox newer_than:14d
    GMAIL_EXTRACT:in:sent newer_than:30d
    GMAIL_EXTRACT:in:inbox is:unread newer_than:7d
    GMAIL_EXTRACT:in:sent newer_than:60d subject:sponsor
  Prefer GMAIL_EXTRACT when user says inbox, sent, mailbox, unread, emails from/to, filter mail.
- DRAFT_EMAIL: create/save one OR MANY Gmail drafts (not send, not schedule).
  Compact JSON ONLY — never include html_body.
  Single: {"recipient_email":"a@b.com","subject":"Hello"}
  Multi: {"batch":true,"recipient_emails":["a@b.com","b@c.com"],"subject":"Hello"}
  From last prospect search: {"batch":true,"from_prospects":true,"subject":"Hello"}
  Personalized follow-ups to last inbox/sent pull:
    {"batch":true,"from_mailbox":true,"subject":"Re: {prior_subject}"}
  Optional filter on last mailbox: {"batch":true,"from_mailbox":true,"mailbox_filter":"sponsor"}
- SEND_EMAIL: send email(s) now. Same compact JSON shapes as DRAFT_EMAIL (including from_mailbox).
- SCHEDULE_EMAIL: schedule/queue for later. Compact JSON with recipient_email, subject, send_at.
  NEVER include html_body in routing lines.
- Prefer DRAFT_EMAIL for draft/compose/save draft / follow-up drafts.
- Prefer SEND_EMAIL for send now / fire off.
- Prefer SCHEDULE_EMAIL for schedule/send later/queue.
- If user wants bulk personalized follow-ups to people from inbox/sent / last mail pull, use
  DRAFT_EMAIL (or SEND_EMAIL) with batch:true and from_mailbox:true.
- Chat may include file attachments; do not mention them in the routing line.
- When prefixed with [ATTACHED FILES: ...], files are already uploaded — do not ask to attach.

Output NOTHING except the single routing line.
"""

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_TEMPLATE_KEYS = (
    "first_name",
    "name",
    "title",
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
- For bulk follow-ups from inbox/sent, use placeholders
  {{first_name}}, {{name}}, {{company}}, {{prior_subject}}, {{prior_summary}}
  so each recipient gets a personalized message.

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
    mailbox_messages: Optional[list[dict[str, Any]]] = None,
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
    if payload.get("from_prospects") or payload.get("use_prospects"):
        use_prospects = True

    use_mailbox = bool(
        payload.get("from_mailbox")
        or payload.get("use_mailbox")
        or payload.get("follow_up")
        or re.search(
            r"\b(follow[- ]?ups?|from (my )?(inbox|sent|mailbox)|last (mail|inbox|sent|extract)|everyone (i|we) (emailed|contacted))\b",
            user_msg,
            re.I,
        )
    )
    if payload.get("from_mailbox") or payload.get("use_mailbox") or payload.get("follow_up"):
        use_mailbox = True

    jobs: list[dict[str, Any]] = []

    if use_mailbox:
        msgs = list(mailbox_messages or [])
        filt = str(payload.get("mailbox_filter") or payload.get("filter") or "").strip()
        if not filt:
            # Try to pull a filter phrase after "about/regarding/filter"
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
        if re.search(r"\binbox\b", user_msg, re.I) and not re.search(r"\bsent\b", user_msg, re.I):
            prefer = "inbox"
        elif re.search(r"\bsent\b", user_msg, re.I) and not re.search(r"\binbox\b", user_msg, re.I):
            prefer = "sent"
        contacts = contacts_from_mailbox(msgs, prefer=prefer)
        for p in contacts:
            job = {
                "recipient_email": p.get("email"),
                "recipient_name": p.get("name") or "",
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

    scraped = _EMAIL_RE.findall(user_msg)
    if len(scraped) > 1:
        for e in scraped:
            if e not in emails:
                emails.append(e)
    elif not emails and scraped:
        emails.extend(scraped)

    seen: set[str] = set()
    uniq: list[str] = []
    for e in emails:
        key = e.lower()
        if key in seen:
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
        lines.append(f"{i}. [{box}] {date} | {who} | {subj}")
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
) -> list[str]:
    """Pull CC addresses from seed JSON and phrases like 'cc a@b.com'."""
    found: list[str] = []
    seed = seed or {}
    for key in ("cc", "cc_emails", "cc_email"):
        val = seed.get(key)
        if isinstance(val, str):
            found.extend(_EMAIL_RE.findall(val))
        elif isinstance(val, list):
            for item in val:
                found.extend(_EMAIL_RE.findall(str(item)))

    msg = user_msg or ""
    for m in re.finditer(
        r"\bcc(?:\s*[:=]|\s+to)?\s+([^\n|;]+)",
        msg,
        re.I,
    ):
        found.extend(_EMAIL_RE.findall(m.group(1)))
    # Also: "copy alice@x.com" / "with cc alice@x.com"
    for m in re.finditer(
        r"\b(?:copy|carbon\s+copy)\s+([^\n|;]+)",
        msg,
        re.I,
    ):
        found.extend(_EMAIL_RE.findall(m.group(1)))

    found.extend(default_cc_emails())
    exclude = {e.lower() for e in (exclude or set())}
    out: list[str] = []
    seen: set[str] = set()
    for e in found:
        key = e.lower()
        if key in seen or key in exclude:
            continue
        seen.add(key)
        out.append(e)
    return out


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


def _deliver_job(
    job: dict[str, Any],
    *,
    want_send: bool,
    user_msg: str,
) -> tuple[dict[str, Any], bool]:
    """Create a new draft (default) or send. Returns (result, did_send)."""
    do_send = want_send and not _prefer_draft_over_send(user_msg, True)
    kwargs = {
        "to": job["recipient_email"],
        "subject": job.get("subject") or "(no subject)",
        "html_body": job.get("html_body") or "",
        "recipient_name": job.get("recipient_name") or "",
        "attachments": job.get("attachments"),
        "campaign": job.get("campaign"),
        "source": job.get("source"),
        "from_email": job.get("from_email") or default_from_email(),
        "cc": job.get("cc") or [],
        "include_signature": True,
    }
    if do_send:
        return send_email(**kwargs), True
    return create_draft(**kwargs), False


def _mail_headers(
    user_msg: str,
    *,
    seed: Optional[dict[str, Any]] = None,
    to_emails: Optional[list[str]] = None,
) -> dict[str, Any]:
    exclude = {e.lower() for e in (to_emails or [])}
    return {
        "from_email": default_from_email(),
        "cc": _extract_cc_emails(user_msg, seed=seed, exclude=exclude),
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

    routing = route(_route_user_msg(user_msg, chat_attachments), history)
    sources: list[dict[str, Any]] = []
    meta_routing = routing
    need_file = False

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

    # Heuristic: LinkedIn URL → enrich (optionally continue to draft/send)
    linkedin_url = extract_linkedin_url(user_msg or "")
    wants_email_after_enrich = bool(
        linkedin_url
        and re.search(
            r"\b(send|draft|email|mail|outreach|write (to|them|him|her))\b",
            user_msg or "",
            re.I,
        )
    )
    if linkedin_url and (
        routing == "CHAT"
        or routing.startswith("CHAT")
        or routing.startswith("PROSPECT_SEARCH")
        or (
            wants_email_after_enrich
            and routing.startswith(("DRAFT_EMAIL", "SEND_EMAIL"))
        )
    ):
        first, last = names_from_linkedin_url(linkedin_url)
        company_m = re.search(
            r"\b(?:at|@|company)\s+([A-Za-z0-9&.\- ]{2,60})",
            user_msg or "",
            re.I,
        )
        ident: dict[str, Any] = {"linkedin_url": linkedin_url}
        if first:
            ident["first_name"] = first
        if last:
            ident["last_name"] = last
        if company_m:
            ident["company"] = company_m.group(1).strip(" .,")
        routing = "PROSPECT_ENRICH:" + json.dumps(ident, ensure_ascii=False)
        meta_routing = routing

    # Heuristic: mission/demographic research → web find orgs → ZoomInfo → optional draft
    if wants_research_then_zoom(user_msg or "") and (
        routing == "CHAT"
        or routing.startswith("CHAT")
        or routing.startswith("PROSPECT_SEARCH")
        or routing.startswith(("DRAFT_EMAIL", "SEND_EMAIL"))
    ):
        draft_flag = bool(
            re.search(r"\b(draft|write|compose|personaliz)\b", user_msg or "", re.I)
        )
        send_flag = bool(
            re.search(r"\b(send now|email them now|fire off)\b", user_msg or "", re.I)
            and not re.search(r"\bdraft\b", user_msg or "", re.I)
        )
        routing = "RESEARCH_THEN_ZOOM:" + json.dumps(
            {
                "org_limit": 8,
                "contacts_per_org": 3,
                "draft": draft_flag or send_flag or bool(
                    re.search(r"\b(email|outreach)\b", user_msg or "", re.I)
                ),
                "send": send_flag,
            },
            ensure_ascii=False,
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
            hits = mem.search(user_msg, k=5)
            ctx = mem.format_for_prompt(hits)
            system = (
                "Answer using ONLY the memory context below when possible. "
                "Cite with [n] markers.\n\n" + ctx
            )
            if used_docs:
                system += (
                    "\n\nAlso use this uploaded file context when relevant:\n"
                    + doc_context
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
            org_limit = int(opts.get("org_limit") or 8)
            per_org = int(opts.get("contacts_per_org") or 3)
            do_draft = bool(opts.get("draft"))
            do_send = bool(opts.get("send"))
            if re.search(r"\b(draft|write|compose|personaliz|email|outreach)\b", user_msg or "", re.I):
                do_draft = True
            if re.search(r"\b(send now|email them now|fire off)\b", user_msg or "", re.I) and not re.search(
                r"\bdraft\b", user_msg or "", re.I
            ):
                do_send = True

            yield (
                "**Step 1/3 — Web research:** finding NGOs/orgs that match your "
                "mission filters (not raw ZoomInfo demographics)…\n"
            )
            try:
                pipeline = run_research_then_zoom(
                    user_msg, org_limit=org_limit, contacts_per_org=per_org
                )
            except Exception as e:
                print(f"[router] research pipeline error: {e}", file=sys.stderr)
                yield f"Research pipeline failed: {e}\n"
                pipeline = {
                    "organizations": [],
                    "contacts": [],
                    "sources": [],
                    "notes": "",
                }

            orgs = pipeline.get("organizations") or []
            contacts = pipeline.get("contacts") or []
            web_sources = pipeline.get("sources") or []
            sources.extend(web_sources)

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
                "\n**Step 2/3 — ZoomInfo + public emails:** looking up contacts "
                "(ZoomInfo first, then public site emails if needed)…\n"
            )
            with_email = [c for c in contacts if (c.get("email") or "").strip()]
            research_only = [c for c in contacts if c.get("research_only")]
            people = [c for c in contacts if not c.get("research_only")]
            prospect_out = people or contacts

            if people:
                yield (
                    f"ZoomInfo returned **{len(people)}** contacts "
                    f"(**{len(with_email)}** with email).\n\n"
                )
                for i, p in enumerate(people[:20], 1):
                    yield f"{i}. {prospect_to_text(p)}\n"
                    if p.get("org_focus"):
                        yield f"   Program fit: {p.get('org_focus')}\n"
            else:
                yield "ZoomInfo had little/no contact coverage for these orgs.\n"
            if research_only:
                yield (
                    f"\n_{len(research_only)} orgs saved from web research without "
                    "ZoomInfo people (you can enrich later by LinkedIn/domain)._\n"
                )

            try:
                auto_ingest_prospects(
                    [c for c in prospect_out if not c.get("research_only")]
                )
            except Exception as e:
                print(f"[router] research auto-ingest error: {e}", file=sys.stderr)

            # Step 3: personalized drafts for contacts with email (review before send)
            if (do_draft or do_send) and with_email:
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

Use placeholders exactly: {{first_name}}, {{company}}, {{title}}, {{org_focus}}
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
                )
                payload = {
                    "batch": True,
                    "from_prospects": True,
                    "subject": subject,
                    "html_body": html_body,
                    "source": "research_then_zoom",
                    "from_email": headers["from_email"],
                    "cc": headers["cc"],
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
                )
                ok_n = 0
                for job in jobs:
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
                        ok_n += 1
                        cc_note = f" cc {out.get('cc')}" if out.get("cc") else ""
                        yield (
                            f"- {'Sent' if did_send else 'Drafted'} → "
                            f"**{job.get('recipient_email')}** "
                            f"(from {out.get('from') or headers['from_email']}{cc_note})\n"
                        )
                    except Exception as e:
                        yield f"- Failed {job.get('recipient_email')}: {e}\n"
                yield (
                    f"\nDone: **{ok_n}** "
                    f"{'emails sent' if want_send else 'new Gmail drafts created for review'}.\n"
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
                f"Contacts:\n{json.dumps(people[:15], default=str)[:4000]}"
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
            # Provider selection: ZoomInfo-first by default; honor explicit providers
            providers = q.pop("providers", None) or q.pop("provider", None)
            if isinstance(providers, str):
                providers = [providers]
            if not providers:
                if re.search(r"\bzoom\s*info\b|\bzi\b", user_msg or "", re.I):
                    providers = ("zoominfo",)
                else:
                    providers = ("zoominfo", "apollo", "rocketreach")
            limit = int(q.pop("limit", None) or q.pop("limit_per_provider", None) or 15)
            yield f"Searching **{', '.join(providers)}**…\n"
            results = search_all(
                q, providers=tuple(providers), limit_per_provider=limit
            )
            ok = [p for p in results if not p.get("error")]
            errs = [p for p in results if p.get("error")]
            prospect_out = ok
            saved_ids: list[str] = []
            if ok:
                try:
                    saved_ids = auto_ingest_prospects(ok)
                except Exception as e:
                    print(f"[router] auto-ingest prospects error: {e}", file=sys.stderr)
            ctx_lines = []
            for i, p in enumerate(ok[:25], 1):
                ctx_lines.append(f"{i}. {prospect_to_text(p)}")
            for e in errs[:5]:
                ctx_lines.append(f"ERROR [{e.get('source')}]: {e.get('error')}")
            if not ok:
                yield (
                    "No prospects returned. "
                    + (f"Errors: {errs}" if errs else "Try a clearer title/company.")
                )
            else:
                with_email = sum(1 for p in ok if (p.get("email") or "").strip())
                yield (
                    f"Found **{len(ok)}** contacts "
                    f"(**{with_email}** with email) via {', '.join(providers)}.\n\n"
                )
                yield "\n".join(ctx_lines[:20])
                if saved_ids:
                    yield f"\n\nAuto-saved **{len(saved_ids)}** contacts to memory."
                yield (
                    "\n\nNext: `draft emails to all these prospects` or "
                    "`send personalized emails to this ZoomInfo list`."
                )
            system = (
                "Summarize these prospect search results for the user. "
                "Highlight emails when present. If emails exist, remind them they can "
                "bulk draft/send personalized emails to this list.\n\n"
                + "\n\n".join(ctx_lines)
            )
            if used_docs:
                system += "\n\nUploaded file context:\n" + doc_context
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
                    "count": len(ok),
                    "providers": list(providers),
                }
            )

        elif routing.startswith("PROSPECT_ENRICH:"):
            ident = _parse_json_tail(routing, "PROSPECT_ENRICH:") or {}
            # Pull LinkedIn / company from the user message when JSON is sparse
            li = (
                ident.get("linkedin_url")
                or ident.get("linkedin")
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
                    r"\b(?:at|@|company)\s+([A-Za-z0-9&.\- ]{2,60})",
                    user_msg or "",
                    re.I,
                )
                if company_m:
                    ident["company"] = company_m.group(1).strip(" .,")
            if not ident:
                ident = {"name": user_msg}

            yield "Enriching contact…\n"
            result = enrich_fallthrough(ident)
            if result and not result.get("error"):
                prospect_out = [result]
                try:
                    auto_ingest_prospects([result])
                except Exception as e:
                    print(f"[router] enrich auto-ingest error: {e}", file=sys.stderr)
                yield (
                    f"Matched **{result.get('name') or 'contact'}**"
                    + (f" · {result.get('title')}" if result.get("title") else "")
                    + (f" @ {result.get('company')}" if result.get("company") else "")
                    + (
                        f"\nEmail: `{result.get('email')}`"
                        if result.get("email")
                        else "\n_(no email found)_"
                    )
                    + "\n"
                )

            # Same-turn draft after LinkedIn enrich (review before send)
            if (
                wants_email_after_enrich
                and result
                and (result.get("email") or "").strip()
            ):
                want_send = bool(
                    re.search(
                        r"\b(send now|email them now|fire off|actually send)\b",
                        user_msg or "",
                        re.I,
                    )
                )
                seed = {
                    "recipient_email": result.get("email"),
                    "recipient_name": result.get("name") or "",
                    "batch": False,
                }
                payload = _extract_email_job(
                    user_msg,
                    history=history,
                    seed=seed,
                    for_schedule=False,
                    document_context=doc_context,
                )
                payload["recipient_email"] = result.get("email")
                payload.setdefault("recipient_name", result.get("name") or "")
                headers = _mail_headers(
                    user_msg,
                    seed=payload,
                    to_emails=[result.get("email") or ""],
                )
                payload["from_email"] = headers["from_email"]
                payload["cc"] = headers["cc"]
                if email_atts:
                    payload["attachments"] = email_atts
                    consumed_attachments = True
                jobs = _build_draft_jobs(
                    payload,
                    user_msg,
                    history=history,
                    prospects=[result],
                    mailbox_messages=mailbox_messages,
                )
                if not jobs:
                    jobs = [
                        {
                            "recipient_email": result.get("email"),
                            "recipient_name": result.get("name") or "",
                            "subject": payload.get("subject") or "(no subject)",
                            "html_body": payload.get("html_body")
                            or payload.get("body")
                            or f"<p>{user_msg}</p>",
                        }
                    ]
                verb = "Sending" if want_send else "Creating draft"
                yield (
                    f"\n{verb} to **{result.get('email')}** "
                    f"from **{headers['from_email']}**…\n"
                )
                for job in jobs:
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
                        yield (
                            f"- {'Sent' if did_send else 'Drafted'}: "
                            f"{json.dumps(out, default=str)}\n"
                        )
                    except Exception as e:
                        yield f"- Failed for {job.get('recipient_email')}: {e}\n"
                if chat_attachments and not email_atts:
                    yield (
                        _attach_note(
                            chat_attachments,
                            used_document_context=used_docs,
                            attached_to_email=False,
                        )
                        + "\n"
                    )
            else:
                system = (
                    "Present this enriched prospect clearly. "
                    "If an email is present, remind the user they can say "
                    "`draft email to them` or `send email to them`.\n\n"
                    + json.dumps(result, default=str)[:6000]
                )
                if used_docs:
                    system += "\n\nUploaded file context:\n" + doc_context
                for chunk in chat_grounded(
                    user_msg, history=history, system=system, use_search=False
                ):
                    if isinstance(chunk, dict) and "__meta__" in chunk:
                        sources = chunk["__meta__"].get("sources") or []
                    else:
                        yield chunk

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
                    "\n\nNext: ask me to **draft personalized follow-ups** to these "
                    "(optionally filtered), e.g. "
                    "`draft follow-ups to everyone in this list about next steps`."
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
            # Prefer mailbox follow-ups when user asks and we have a prior pull
            if mailbox_messages and re.search(
                r"\b(follow[- ]?up|from (this|the|my) (list|inbox|sent|mailbox)|everyone (here|in (the|this) list))\b",
                user_msg or "",
                re.I,
            ):
                seed = {**seed, "batch": True, "from_mailbox": True}
            # Bulk to last ZoomInfo / prospect search
            if prospects and re.search(
                r"\b(these prospects|this list|all (these |the )?prospects|"
                r"zoominfo list|everyone (we |you )?found|all of them)\b",
                user_msg or "",
                re.I,
            ):
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

            jobs = _build_draft_jobs(
                payload,
                user_msg,
                history=history,
                prospects=prospects,
                mailbox_messages=mailbox_messages,
            )
            headers = _mail_headers(
                user_msg,
                seed=payload,
                to_emails=[j.get("recipient_email") or "" for j in jobs],
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
                    job = _stamp_mail_fields(
                        job,
                        from_email=headers["from_email"],
                        cc=headers["cc"],
                        attachments=email_atts,
                    )
                    out, did_send = _deliver_job(
                        job, want_send=want_send, user_msg=user_msg
                    )
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
                for r in ok[:50]:
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
                if not want_send:
                    yield "\nOpen Gmail → Drafts to review, then send.\n"
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
            # CHAT (default) — uploaded files are context for ANY question
            if att_names:
                system = (
                    "The user uploaded these files for context (any topic — not only email): "
                    f"{', '.join(att_names)}. "
                    "Do NOT ask them to re-upload. Use the extracted content below to answer. "
                    "If they later ask to draft/send an email and want the file attached, "
                    "the same staged files will be attached automatically.\n\n"
                    f"{doc_context or '(binary/attach-only file — note it exists)'}"
                )
            else:
                system = (
                    "Files can be uploaded via the chat paperclip for context. "
                    "If they ask to attach a file to an email and none is uploaded yet, "
                    "ask them to use the paperclip first. Do not invent file contents."
                )
            for chunk in chat_grounded(
                user_msg, history=history, system=system, use_search=not bool(att_names)
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
            "routing": meta_routing,
            "sources": sources,
            "consumed_attachments": consumed_attachments,
            "need_file": need_file,
            "mailbox_messages": mailbox_out or None,
            "prospects": prospect_out or None,
        }
    }