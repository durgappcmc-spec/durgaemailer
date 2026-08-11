# NOTE: Intent planner classifies From / To / CC / ignore and which specialist agents to run.
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Optional

from agent.limits import (
    DEFAULT_CONTACTS_PER_ORG,
    DEFAULT_ORGS,
    DEFAULT_SEARCH_LIMIT,
    MAX_EMAILS,
    parse_research_limits,
)
from core.llm import extract_json
from gmail_client.send import default_cc_emails, default_from_email

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

_CC_BLOCK_RE = re.compile(
    r"\b(?:"
    r"cc(?:\s*[:=]|\s+to)?|"
    r"c\.?\s*c\.?|"
    r"carbon\s+copy|"
    r"copy(?:\s+to)?"
    r")\s+(.+?)(?="
    r"\b(?:to|from|subject|attach|draft|send|ignore|skip|except)\b|"
    r"$)",
    re.I | re.S,
)

_TO_BLOCK_RE = re.compile(
    r"\b(?:"
    r"to(?:\s*[:=])?|"
    r"email(?:\s+to)?|"
    r"mail(?:\s+to)?|"
    r"recipient(?:s)?|"
    r"send(?:\s+to)?"
    r")\s+(.+?)(?="
    r"\b(?:cc|c\.?\s*c\.?|carbon\s+copy|copy(?:\s+to)?|from|subject|attach|ignore|skip)\b|"
    r"$)",
    re.I | re.S,
)

_FROM_BLOCK_RE = re.compile(
    r"\b(?:"
    r"from(?:\s*[:=])?|"
    r"as(?:\s*[:=])?|"
    r"using(?:\s+[\"']?from)?|"
    r"send(?:\s+as)|"
    r"on\s+behalf\s+of"
    r")\s+(.+?)(?="
    r"\b(?:to|cc|c\.?\s*c\.?|subject|attach|draft|ignore)\b|"
    r"$)",
    re.I | re.S,
)

_PLAN_SYSTEM = """You are Relay's request planner for a CSR outreach app (Karuna Media).
Return JSON only. Classify what the user wants — do NOT invent NGO partnership searches
just because they mention csr@ or Karuna.

Actions (pick one):
- chat: Q&A / writing help / clarify
- memory: recall saved notes/prospects
- research_then_zoom: ONLY when they want to DISCOVER orgs matching a mission
  (e.g. find NGOs for girls 16+ skilling) then ZoomInfo contacts / drafts
- prospect_search: ZoomInfo/Apollo search with known company/title filters
- prospect_enrich: enrich one person (LinkedIn URL or name+company)
- gmail_extract: read inbox/sent
- draft_email: create Gmail draft(s) for review
- send_email: send now (rare)
- schedule_email: queue for later

Critical:
- csr@karunamedia.org / "from CSR" / "as CSR" means the SENDER identity, NOT a search for CSR NGOs.
- If they already have contacts/prospects/list and ask to draft, use draft_email (agents: gmail).
- Put every CC address in cc (array). Never drop the second CC.
- If the user names people for CC without emails (e.g. "cc Deepti and Rahul"),
  resolve known aliases: Deepti → deepti.87.srivastava@gmail.com,
  Raahul/Rahul → raahul.ppcm@gmail.com. Put the resolved emails in cc.
- Put ignored addresses in ignore_emails when they say ignore/skip/don't use/except.
- from_email defaults to csr@karunamedia.org for outreach.
- Prefer draft over send unless they say send now.
- Honor requested volumes: org_limit / contacts / emails up to 100 when the user asks
  (e.g. "100 emails", "40 NGOs", "as many as needed"). Do not default to tiny 8–10 lists.
- If they ask to create an email LIKE one previously SENT to a company
  (e.g. "create email like sent to IndiaMART" or "similar to the email we sent to X"),
  use draft_email with agents ["gmail","web_research"]. Set like_sent_to to that company
  and like_sent_for to the new target company when named (e.g. "for Flipkart").
  Do NOT use research_then_zoom for this — it is style-clone + alignment research, not NGO discovery.
- Also support recipient email as the Sent reference:
  "create draft email like info@magicbusindia.org in sent items" → like_sent_to=that email
  (do NOT put that address in to_emails — it is the template source, not the new To).
- If they give a Gmail message id / "email id" / list number from a prior Sent pull
  (e.g. "create draft from message id 18abc…" or "draft like #2 from sent"),
  use draft_email and set like_sent_message_id. Read like_sent_to / like_sent_for from
  recent chat when they say "like that" / "same as before" / "for Flipkart" without repeating the company.
- Always use recent chat history for recipients, company names, and which prior email to clone.
"""


@dataclass
class EmailRoles:
    from_email: str = ""
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    ignore: list[str] = field(default_factory=list)


@dataclass
class IntentPlan:
    action: str = "chat"
    agents: list[str] = field(default_factory=list)
    from_email: str = ""
    to_emails: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    ignore_emails: list[str] = field(default_factory=list)
    draft: bool = False
    send: bool = False
    reason: str = ""
    org_limit: int = DEFAULT_ORGS
    contacts_per_org: int = DEFAULT_CONTACTS_PER_ORG
    search_limit: int = DEFAULT_SEARCH_LIMIT
    email_limit: int = MAX_EMAILS
    like_sent_to: str = ""
    like_sent_for: str = ""
    like_sent_message_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def non_recipient_emails(self) -> set[str]:
        out = {e.lower() for e in self.ignore_emails}
        out |= {e.lower() for e in self.cc}
        fe = (self.from_email or default_from_email()).lower()
        if fe:
            out.add(fe)
        out.add("csr@karunamedia.org")
        return out


def _uniq(emails: list[str], *, exclude: Optional[set[str]] = None) -> list[str]:
    exclude = {e.lower() for e in (exclude or set())}
    out: list[str] = []
    seen: set[str] = set()
    for e in emails:
        key = (e or "").strip().lower()
        if not key or key in seen or key in exclude:
            continue
        seen.add(key)
        out.append(e.strip())
    return out


def ignored_emails(user_msg: str) -> list[str]:
    """Emails the user explicitly said to ignore / skip / not use."""
    msg = user_msg or ""
    found: list[str] = []
    for m in re.finditer(
        r"\b(?:ignore|skip|exclude|omit|"
        r"don'?t\s+(?:use|email|contact|include|send)|"
        r"do\s+not\s+(?:use|email|contact|include|send)|"
        r"except(?:\s+for)?)\s+"
        r"(.+?)(?=\b(?:cc|c\.?\s*c\.?|to|from|subject|and\s+then|attach|draft|send)\b|$)",
        msg,
        re.I | re.S,
    ):
        found.extend(_EMAIL_RE.findall(m.group(1)))
    # "not foo@bar.com"
    for m in re.finditer(
        r"\bnot\s+(?:to\s+|for\s+)?([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})",
        msg,
        re.I,
    ):
        found.append(m.group(1))
    return _uniq(found)


def classify_email_roles(user_msg: str) -> EmailRoles:
    """Heuristic From / To / CC / ignore from the user message."""
    from agent.contact_aliases import learn_aliases_from_text, resolve_names_in_text

    msg = user_msg or ""
    # Learn "Raahul as <email>" style mappings from this turn
    learn_aliases_from_text(msg)

    ignore = ignored_emails(msg)
    ignore_set = {e.lower() for e in ignore}

    from_cands: list[str] = []
    for m in _FROM_BLOCK_RE.finditer(msg):
        from_cands.extend(_EMAIL_RE.findall(m.group(1)))
    if re.search(
        r"\b(csr@karunamedia\.org|as\s+csr|from\s+csr|using\s+csr)\b", msg, re.I
    ):
        from_cands.append("csr@karunamedia.org")
    from_email = (from_cands[0] if from_cands else default_from_email()).strip()

    cc: list[str] = []
    cc_spans: list[str] = []
    for m in _CC_BLOCK_RE.finditer(msg):
        span = m.group(1)
        cc_spans.append(span)
        cc.extend(_EMAIL_RE.findall(span))
    for m in re.finditer(r"\bcc(?:\s*[:=]|\s+to)?\s*([^\n]+)", msg, re.I):
        span = m.group(1)
        cc_spans.append(span)
        cc.extend(_EMAIL_RE.findall(span))
    for m in re.finditer(r"\b(?:copy|carbon\s+copy)\s+([^\n]+)", msg, re.I):
        span = m.group(1)
        cc_spans.append(span)
        cc.extend(_EMAIL_RE.findall(span))
    # Resolve nicknames in CC phrases: "cc deepti and rahul"
    for span in cc_spans:
        cc.extend(resolve_names_in_text(span))
    cc.extend(default_cc_emails())
    cc = _uniq(cc, exclude=ignore_set | {from_email.lower()})

    to: list[str] = []
    for m in _TO_BLOCK_RE.finditer(msg):
        span = m.group(1)
        to.extend(_EMAIL_RE.findall(span))
        to.extend(resolve_names_in_text(span))
    to = _uniq(
        to,
        exclude=ignore_set | {from_email.lower()} | {e.lower() for e in cc},
    )

    return EmailRoles(
        from_email=from_email or default_from_email(),
        to=to,
        cc=cc,
        ignore=ignore,
    )


def parse_gmail_message_id(text: str) -> str:
    """Extract a Gmail API message id from user/assistant text."""
    msg = text or ""
    patterns = [
        r"\b(?:message[_ -]?id|msg[_ -]?id|email[_ -]?id|gmail[_ -]?id)\s*[:=#]?\s*[`'\"]?([A-Za-z0-9_\-]{10,})[`'\"]?",
        r"\bid\s*[:=]\s*[`'\"]?([A-Za-z0-9_\-]{10,})[`'\"]?",
        r"\bid=`([A-Za-z0-9_\-]{10,})`",
        r"\b(?:from|using|based on)\s+(?:sent\s+)?(?:message|email|mail)\s+[`]?([A-Za-z0-9_\-]{16,})[`]?",
    ]
    for pat in patterns:
        m = re.search(pat, msg, re.I)
        if m:
            mid = (m.group(1) or "").strip()
            # Avoid catching bare emails / URLs
            if "@" in mid or "." in mid:
                continue
            if re.fullmatch(r"[A-Za-z0-9_\-]{10,}", mid):
                return mid
    return ""


def parse_mailbox_list_index(text: str) -> Optional[int]:
    """Parse 'email #3' / 'number 2 from sent' / 'the 1st one' → 1-based index."""
    msg = text or ""
    patterns = [
        r"(?:^|[\s])#\s*(\d{1,3})\b(?:\s+from\s+(?:sent|inbox|list|above))?",
        r"\b(?:email|message|mail|item|row)\s*#?\s*(\d{1,3})\b",
        r"\b(?:number|no\.?)\s*(\d{1,3})\b(?:\s+from\s+(?:sent|inbox|list|above|the list))?",
        r"\bthe\s+(\d{1,2})(?:st|nd|rd|th)\s+(?:one|email|message)\b",
    ]
    for pat in patterns:
        m = re.search(pat, msg, re.I)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 200:
                return n
    return None


def looks_like_history_email_clone(user_msg: str) -> bool:
    """True when user refers to a prior chat email without naming the company again."""
    msg = user_msg or ""
    return bool(
        re.search(
            r"\b("
            r"like that|"
            r"same as (that|before|the previous|earlier)|"
            r"as before|"
            r"previous (email|one|sent|message)|"
            r"that (sent )?email|"
            r"the one (we|you) (sent|showed|found)|"
            r"based on (that|the|previous|this)|"
            r"from (that|this|the) (sent )?email|"
            r"using (that|this|the) (sent )?(email|message)|"
            r"like the (previous|last|earlier) (one|email|message)"
            r")\b",
            msg,
            re.I,
        )
    )


def resolve_like_sent_from_history(
    user_msg: str,
    history: Optional[list[dict[str, str]]] = None,
    *,
    like_sent_to: str = "",
    like_sent_for: str = "",
    like_sent_message_id: str = "",
) -> dict[str, str]:
    """Fill like-sent reference/target/message_id from current msg + recent chat."""
    out = {
        "reference": (like_sent_to or "").strip(),
        "target": (like_sent_for or "").strip(),
        "message_id": (like_sent_message_id or "").strip(),
    }
    parsed = parse_like_sent_request(user_msg or "")
    if parsed:
        out["reference"] = (parsed.get("reference") or out["reference"]).strip()
        out["target"] = (parsed.get("target") or out["target"]).strip()
    mid = parse_gmail_message_id(user_msg or "")
    if mid:
        out["message_id"] = mid

    # Explicit "for Company" when cloning from history / id
    if not out["target"]:
        fm = re.search(
            r"\b(?:for|about|targeting)\s+([A-Za-z0-9][A-Za-z0-9&.\'\-]*(?:\s+[A-Za-z0-9][A-Za-z0-9&.\'\-]*){0,4})",
            user_msg or "",
            re.I,
        )
        if fm:
            cand = fm.group(1).strip(" .,;:")
            if cand.lower() not in ("me", "us", "them", "review", "csr"):
                out["target"] = cand

    need_hist = (
        looks_like_history_email_clone(user_msg or "")
        or bool(out["message_id"])
        or bool(out["target"])
        or bool(out["reference"])
    )
    if not need_hist or not history:
        return out

    for m in reversed(history[-16:]):
        content = str(m.get("content") or "")
        if not content:
            continue
        if not out["message_id"]:
            hid = parse_gmail_message_id(content)
            if hid:
                out["message_id"] = hid
        if not out["reference"]:
            p = parse_like_sent_request(content)
            if p and p.get("reference"):
                out["reference"] = p["reference"]
                if not out["target"] and p.get("target"):
                    out["target"] = p["target"]
            else:
                m_like = re.search(
                    r"like sent to:\s*([^→\n]+?)(?:\s*→\s*for\s+([^\n]+))?$",
                    content,
                    re.I | re.M,
                )
                if m_like:
                    out["reference"] = m_like.group(1).strip()
                    if m_like.group(2) and not out["target"]:
                        out["target"] = m_like.group(2).strip()
                else:
                    m_ref = re.search(
                        r"\*\*Like-sent:\*\*\s*finding Gmail sent to\s+\*\*([^*]+)\*\*",
                        content,
                        re.I,
                    )
                    if m_ref:
                        out["reference"] = m_ref.group(1).strip()
                    m_adapt = re.search(
                        r"adapting for\s+\*\*([^*]+)\*\*",
                        content,
                        re.I,
                    )
                    if m_adapt and not out["target"]:
                        out["target"] = m_adapt.group(1).strip()
        if out["reference"] and (out["target"] or out["message_id"]):
            break
        if out["reference"] and out["target"]:
            break
    return out


def parse_like_sent_request(user_msg: str) -> Optional[dict[str, str]]:
    """Detect 'create email like sent to IndiaMART' or 'like info@x.org in sent'.

    Returns {"reference": "...", "target": "..."} or None.
    reference may be a company name or a recipient email address.
    """
    msg = (user_msg or "").strip()
    if not msg:
        return None

    email_pat = r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})"
    # Company tokens; do not consume "for/about/targeting/to …" as part of the name
    company = (
        r"([A-Za-z0-9][A-Za-z0-9&.\'\-]*"
        r"(?:\s+(?!for\b|about\b|targeting\b|to\b|cc\b|from\b|and\b|with\b|"
        r"in\b|from\b|sent\b|items?\b)"
        r"[A-Za-z0-9][A-Za-z0-9&.\'\-]*){0,6})"
    )
    stop_tail = re.compile(
        r"\s+\b(?:and|with|cc|from|subject|attach|draft|send|please|thanks|"
        r"using|based|that|which|who|to|in|sent|items?)\b.*$",
        re.I,
    )

    def _clean(name: str) -> str:
        name = (name or "").strip(" .,;:!?'\"")
        name = stop_tail.sub("", name).strip(" .,;:")
        return name

    def _target_from_msg(reference: str) -> str:
        for fm in re.finditer(
            rf"\b(?:for|about|targeting)\s+{company}",
            msg,
            re.I,
        ):
            cand = _clean(fm.group(1) or "")
            if cand and cand.lower() != reference.lower() and "@" not in cand:
                return cand
        return ""

    # --- Email-address references (highest priority) ---
    email_patterns = [
        # create draft email like info@x.org in sent [items]
        rf"(?:create|write|draft|compose|make)\s+(?:an?\s+)?(?:similar\s+)?"
        rf"(?:email|mail|message).{{0,60}}?like\s+{email_pat}"
        rf"(?:\s+(?:in|from)\s+(?:the\s+)?sent(?:\s+items?)?)?",
        # like info@x.org in sent / from sent
        rf"(?:like|similar\s+to)\s+{email_pat}"
        rf"(?:\s+(?:in|from)\s+(?:the\s+)?sent(?:\s+items?)?)?",
        # like the one sent to info@x.org
        rf"(?:like|similar\s+to)\s+(?:the\s+)?"
        rf"(?:one\s+|email\s+|mail\s+)?(?:we\s+|you\s+|i\s+)?"
        rf"(?:sent|emailed)\s+to\s+{email_pat}",
        # sent to info@x.org (with draft/create nearby)
        rf"(?:sent|emailed)\s+to\s+{email_pat}",
    ]
    for pat in email_patterns:
        m = re.search(pat, msg, re.I | re.S)
        if not m:
            continue
        # Only treat as like-sent when draft/like/create intent is present
        if not re.search(
            r"\b(draft|create|compose|write|make|like|similar|clone|same\s+as)\b",
            msg,
            re.I,
        ):
            continue
        reference = (m.group(1) or "").strip()
        if reference and "@" in reference:
            return {
                "reference": reference,
                "target": _target_from_msg(reference),
            }

    patterns = [
        # create/draft email like (the one) sent/emailed to X [for Y]
        rf"(?:create|write|draft|compose|make)\s+(?:an?\s+)?(?:similar\s+)?"
        rf"(?:email|mail|message).{{0,80}}?(?:like|similar\s+to).{{0,40}}?"
        rf"(?:the\s+)?(?:one\s+|email\s+|mail\s+)?(?:we\s+|you\s+|i\s+)?"
        rf"(?:sent|emailed)\s+to\s+{company}"
        rf"(?:\s+(?:for|about|targeting)\s+{company})?",
        # like the email we sent to X
        rf"(?:like|similar\s+to)\s+(?:the\s+)?"
        rf"(?:one\s+|email\s+|mail\s+)?(?:we\s+|you\s+|i\s+)?"
        rf"(?:sent|emailed)\s+to\s+{company}"
        rf"(?:\s+(?:for|about|targeting)\s+{company})?",
        # email like sent to X / same as sent to X
        rf"(?:email|mail).{{0,40}}?(?:like|same\s+as).{{0,20}}?"
        rf"(?:sent|emailed)\s+to\s+{company}"
        rf"(?:\s+(?:for|about|targeting)\s+{company})?",
        # create draft like MagicBus in sent
        rf"(?:create|write|draft|compose|make).{{0,40}}?"
        rf"(?:email|mail).{{0,40}}?like\s+{company}"
        rf"\s+(?:in|from)\s+(?:the\s+)?sent(?:\s+items?)?",
    ]
    for pat in patterns:
        m = re.search(pat, msg, re.I | re.S)
        if not m:
            continue
        reference = _clean(m.group(1) or "")
        target = ""
        if m.lastindex and m.lastindex >= 2 and m.group(2):
            target = _clean(m.group(2) or "")
        if not target:
            target = _target_from_msg(reference)
        if reference and len(reference) >= 2 and "@" not in reference:
            return {"reference": reference, "target": target}
    return None


def org_label_from_email(email: str) -> str:
    """magicbusindia.org → magicbusindia (for company-name swap)."""
    email = (email or "").strip()
    if "@" not in email:
        return email
    domain = email.split("@", 1)[1]
    base = domain.split(".")[0].strip()
    return base or email


def looks_like_mission_org_discovery(user_msg: str) -> bool:
    """True only when user wants to discover mission-fit orgs (not CSR-as-sender)."""
    msg = user_msg or ""
    if parse_like_sent_request(msg):
        return False
    if re.search(
        r"\b(from|as|using|send(?:\s+as)?)\s+(csr@|csr\b)|csr@karunamedia\.org",
        msg,
        re.I,
    ) and not re.search(
        r"\b(find|search|discover|look\s*up|research)\b.{0,40}\b("
        r"ngo|ngos|non[\s-]?profit|foundation|skilling|girls?\s+16"
        r")\b",
        msg,
        re.I,
    ):
        return False

    has_mission = bool(
        re.search(
            r"\b(ngo|ngos|non[\s-]?profit|foundation|trust|skilling|vocational|"
            r"livelihood|girls?\s*16|women|16\+|above\s*16|teen\w*|underprivileged|"
            r"education program|empower\w*)\b",
            msg,
            re.I,
        )
    )
    wants_discover = bool(
        re.search(
            r"\b(find|search|discover|list|look\s*up|research|identify|who\s+are|"
            r"which\s+(ngos?|orgs?|organizations)|matching\s+(ngos?|orgs?))\b",
            msg,
            re.I,
        )
    )
    wants_zi = bool(
        re.search(
            r"\b(zoom\s*info|zoominfo|\bzi\b|enrich|find (emails?|contacts?))\b",
            msg,
            re.I,
        )
    )
    return bool(has_mission and (wants_discover or wants_zi))


def _heuristic_plan(user_msg: str) -> IntentPlan:
    roles = classify_email_roles(user_msg)
    vol = parse_research_limits(user_msg)
    msg = user_msg or ""
    agents: list[str] = []
    action = "chat"
    draft = bool(re.search(r"\b(draft|compose|write|save\s+draft)\b", msg, re.I))
    send = bool(
        re.search(
            r"\b(send\s+now|actually\s+send|fire\s+off|send\s+immediately)\b",
            msg,
            re.I,
        )
    )

    like_sent = parse_like_sent_request(msg)
    like_sent_to = (like_sent or {}).get("reference") or ""
    like_sent_for = (like_sent or {}).get("target") or ""
    like_sent_message_id = parse_gmail_message_id(msg)
    clone_from_hist = looks_like_history_email_clone(msg)
    clone_from_index = parse_mailbox_list_index(msg) is not None and bool(
        re.search(r"\b(draft|create|compose|like|based|using)\b", msg, re.I)
    )

    # Never treat the like-sent reference address as the new draft's To
    to_emails = list(roles.to)
    if like_sent_to and "@" in like_sent_to:
        to_emails = [e for e in to_emails if e.lower() != like_sent_to.lower()]

    if like_sent_to or like_sent_message_id or clone_from_hist or clone_from_index:
        action = "send_email" if send and not draft else "draft_email"
        agents = ["gmail", "web_research"]
        draft = action == "draft_email"
    elif looks_like_mission_org_discovery(msg):
        action = "research_then_zoom"
        agents = ["web_research", "zoominfo"]
        if draft or send or re.search(r"\b(email|outreach|personaliz)\b", msg, re.I):
            agents.append("gmail")
            draft = True
    elif re.search(
        r"\b(my inbox|my sent|show (me )?(inbox|sent)|list (inbox|sent)|unread)\b",
        msg,
        re.I,
    ):
        action = "gmail_extract"
        agents = ["gmail"]
    elif re.search(r"linkedin\.com/in/", msg, re.I):
        action = "prospect_enrich"
        agents = ["zoominfo"]
        if draft or send or re.search(r"\b(email|mail|outreach)\b", msg, re.I):
            agents.append("gmail")
            draft = draft or not send
    elif re.search(r"\b(zoom\s*info|zoominfo|apollo|rocketreach|prospect)\b", msg, re.I):
        action = "prospect_search"
        agents = ["zoominfo"]
    elif draft or send or to_emails or re.search(
        r"\b(email|outreach|follow[- ]?up)\b", msg, re.I
    ):
        action = "send_email" if send and not draft else "draft_email"
        agents = ["gmail"]
        draft = action == "draft_email"
    elif re.search(r"\b(memory|saved|what do (we|you) know)\b", msg, re.I):
        action = "memory"
        agents = ["memory"]

    return IntentPlan(
        action=action,
        agents=agents or ["chat"],
        from_email=roles.from_email,
        to_emails=to_emails,
        cc=roles.cc,
        ignore_emails=roles.ignore,
        draft=draft,
        send=send,
        reason="heuristic",
        org_limit=vol["org_limit"],
        contacts_per_org=vol["contacts_per_org"],
        search_limit=vol["search_limit"],
        email_limit=vol["email_limit"],
        like_sent_to=like_sent_to,
        like_sent_for=like_sent_for,
        like_sent_message_id=like_sent_message_id,
    )


def plan_request(
    user_msg: str,
    history: Optional[list[dict[str, str]]] = None,
) -> IntentPlan:
    """LLM + heuristic plan for which agents to invoke and mail headers."""
    base = _heuristic_plan(user_msg)
    hist = ""
    if history:
        # Keep enough context for follow-up drafts (company names, ids, prior bodies)
        parts: list[str] = []
        for m in history[-12:]:
            role = m.get("role") or "?"
            content = (m.get("content") or "").strip()
            if len(content) > 1800:
                content = content[:1800] + "…"
            parts.append(f"{role}: {content}")
        hist = "\n".join(parts)
    prompt = f"""Plan how Relay should handle this user request.

Default from address: {default_from_email()}
Heuristic guess (may be wrong): {json.dumps({
    "action": base.action,
    "agents": base.agents,
    "from_email": base.from_email,
    "to_emails": base.to_emails,
    "cc": base.cc,
    "ignore_emails": base.ignore_emails,
    "draft": base.draft,
    "send": base.send,
    "like_sent_to": base.like_sent_to,
    "like_sent_for": base.like_sent_for,
    "like_sent_message_id": base.like_sent_message_id,
    "org_limit": base.org_limit,
    "contacts_per_org": base.contacts_per_org,
    "search_limit": base.search_limit,
    "email_limit": base.email_limit,
})}

Recent chat (use this for follow-up drafts — recipients, companies, which sent email to clone):
{hist}

User message:
{user_msg}

Return JSON:
{{
  "action": "chat|memory|research_then_zoom|prospect_search|prospect_enrich|gmail_extract|draft_email|send_email|schedule_email",
  "agents": ["web_research","zoominfo","gmail","memory","chat"],
  "from_email": "csr@karunamedia.org",
  "to_emails": ["contact@org.org"],
  "cc": ["a@x.com","b@y.com"],
  "ignore_emails": ["skip@x.com"],
  "draft": true,
  "send": false,
  "like_sent_to": "company name from a prior sent email to clone, or empty",
  "like_sent_for": "new target company to adapt for, or empty",
  "like_sent_message_id": "Gmail message id when user names one, or empty",
  "org_limit": {base.org_limit},
  "contacts_per_org": {base.contacts_per_org},
  "search_limit": {base.search_limit},
  "email_limit": {base.email_limit},
  "reason": "one short sentence"
}}
"""
    try:
        raw = extract_json(prompt, system=_PLAN_SYSTEM, max_tokens=900)
        data = json.loads(raw or "{}")
        if not isinstance(data, dict):
            return base
    except Exception as e:
        print(f"[intent] plan_request failed: {e}", file=sys.stderr)
        return base

    roles = classify_email_roles(user_msg)
    from agent.contact_aliases import resolve_name, resolve_names_in_text

    llm_cc: list[str] = []
    for key in ("cc", "cc_emails"):
        val = data.get(key)
        if isinstance(val, list):
            for x in val:
                s = str(x).strip()
                if _EMAIL_RE.fullmatch(s):
                    llm_cc.append(s)
                else:
                    # LLM may return "Deepti" — resolve alias
                    resolved = resolve_name(s) or resolve_names_in_text(s)
                    if isinstance(resolved, list):
                        llm_cc.extend(resolved)
                    elif resolved:
                        llm_cc.append(resolved)
        elif isinstance(val, str):
            llm_cc.extend(_EMAIL_RE.findall(val))
            llm_cc.extend(resolve_names_in_text(val))
    cc = _uniq(llm_cc + roles.cc + base.cc)

    llm_ignore: list[str] = []
    for key in ("ignore_emails", "ignore", "exclude_emails"):
        val = data.get(key)
        if isinstance(val, list):
            llm_ignore.extend(str(x) for x in val)
        elif isinstance(val, str):
            llm_ignore.extend(_EMAIL_RE.findall(val))
    ignore = _uniq(llm_ignore + roles.ignore + base.ignore_emails)

    llm_to: list[str] = []
    for key in ("to_emails", "to", "recipient_emails"):
        val = data.get(key)
        if isinstance(val, list):
            llm_to.extend(str(x) for x in val)
        elif isinstance(val, str):
            llm_to.extend(_EMAIL_RE.findall(val))
    from_email = (
        data.get("from_email") or roles.from_email or base.from_email or default_from_email()
    ).strip()
    exclude = (
        {e.lower() for e in ignore}
        | {from_email.lower()}
        | {c.lower() for c in cc}
    )
    # Template Sent recipient is never the new draft To
    if base.like_sent_to and "@" in base.like_sent_to:
        exclude.add(base.like_sent_to.lower())
    to_emails = _uniq(llm_to + roles.to + base.to_emails, exclude=exclude)

    action = str(data.get("action") or base.action).strip().lower()
    if action == "research_then_zoom" and not looks_like_mission_org_discovery(
        user_msg
    ):
        action = "draft_email" if (base.draft or data.get("draft")) else base.action
        if action == "research_then_zoom":
            action = "chat"

    agents = data.get("agents")
    if not isinstance(agents, list) or not agents:
        agents = list(base.agents)
    agents = [str(a).lower() for a in agents]

    draft = bool(data.get("draft")) if "draft" in data else base.draft
    send = bool(data.get("send")) if "send" in data else base.send
    if action in ("draft_email", "research_then_zoom") and not send:
        draft = True

    vol = parse_research_limits(user_msg)
    org_limit = int(data.get("org_limit") or vol["org_limit"] or base.org_limit)
    contacts_per_org = int(
        data.get("contacts_per_org") or vol["contacts_per_org"] or base.contacts_per_org
    )
    search_limit = int(
        data.get("search_limit")
        or data.get("limit")
        or vol["search_limit"]
        or base.search_limit
    )
    email_limit = int(
        data.get("email_limit") or vol["email_limit"] or base.email_limit or MAX_EMAILS
    )
    # Prefer explicit user volumes over tiny LLM defaults
    org_limit = max(org_limit, vol["org_limit"])
    search_limit = max(search_limit, vol["search_limit"])
    email_limit = max(email_limit, vol["email_limit"])

    like_sent_to = str(
        data.get("like_sent_to") or base.like_sent_to or ""
    ).strip()
    like_sent_for = str(
        data.get("like_sent_for") or base.like_sent_for or ""
    ).strip()
    like_sent_message_id = str(
        data.get("like_sent_message_id") or base.like_sent_message_id or ""
    ).strip()
    resolved = resolve_like_sent_from_history(
        user_msg,
        history,
        like_sent_to=like_sent_to,
        like_sent_for=like_sent_for,
        like_sent_message_id=like_sent_message_id,
    )
    like_sent_to = resolved.get("reference") or like_sent_to
    like_sent_for = resolved.get("target") or like_sent_for
    like_sent_message_id = resolved.get("message_id") or like_sent_message_id
    if like_sent_to and "@" in like_sent_to:
        to_emails = [
            e for e in to_emails if e.lower() != like_sent_to.lower()
        ]

    if (
        like_sent_to or like_sent_message_id or looks_like_history_email_clone(user_msg)
    ) and action in ("chat", "research_then_zoom", "gmail_extract"):
        action = "draft_email" if not send else "send_email"
        draft = action == "draft_email"
        if "gmail" not in agents:
            agents.append("gmail")
        if "web_research" not in agents:
            agents.append("web_research")

    return IntentPlan(
        action=action,
        agents=agents,
        from_email=from_email,
        to_emails=to_emails,
        cc=cc,
        ignore_emails=ignore,
        draft=draft,
        send=send,
        reason=str(data.get("reason") or "planned"),
        org_limit=min(max(org_limit, 1), 100),
        contacts_per_org=min(max(contacts_per_org, 1), 25),
        search_limit=min(max(search_limit, 1), 100),
        email_limit=min(max(email_limit, 1), MAX_EMAILS),
        like_sent_to=like_sent_to,
        like_sent_for=like_sent_for,
        like_sent_message_id=like_sent_message_id,
        raw=data,
    )


def filter_recipient_emails(
    emails: list[str],
    *,
    plan: IntentPlan,
) -> list[str]:
    """Drop From / CC / ignored addresses that must never be To."""
    return _uniq(emails, exclude=plan.non_recipient_emails())


def plan_summary(plan: IntentPlan) -> str:
    parts = [
        f"**Planner:** `{plan.action}`",
        f"agents: {', '.join(plan.agents) or 'chat'}",
        f"from: {plan.from_email or default_from_email()}",
    ]
    if plan.action == "research_then_zoom":
        parts.append(
            f"volume: ≤{plan.org_limit} orgs × {plan.contacts_per_org}/org "
            f"(≤{plan.email_limit} emails)"
        )
    elif plan.action == "prospect_search":
        parts.append(f"search_limit: {plan.search_limit}")
    elif plan.action in ("draft_email", "send_email"):
        parts.append(f"email_cap: {plan.email_limit}")
    if plan.like_sent_to:
        like_bit = f"like sent to: {plan.like_sent_to}"
        if plan.like_sent_for:
            like_bit += f" → for {plan.like_sent_for}"
        parts.append(like_bit)
    if plan.like_sent_message_id:
        parts.append(f"message_id: `{plan.like_sent_message_id}`")
    if plan.cc:
        parts.append(f"cc: {', '.join(plan.cc)}")
    if plan.ignore_emails:
        parts.append(f"ignoring: {', '.join(plan.ignore_emails)}")
    if plan.reason:
        parts.append(f"— {plan.reason}")
    return " · ".join(parts) + "\n"
