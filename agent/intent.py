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
- Put ignored addresses in ignore_emails when they say ignore/skip/don't use/except.
- from_email defaults to csr@karunamedia.org for outreach.
- Prefer draft over send unless they say send now.
- Honor requested volumes: org_limit / contacts / emails up to 100 when the user asks
  (e.g. "100 emails", "40 NGOs", "as many as needed"). Do not default to tiny 8–10 lists.
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
    msg = user_msg or ""
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
    for m in _CC_BLOCK_RE.finditer(msg):
        cc.extend(_EMAIL_RE.findall(m.group(1)))
    for m in re.finditer(r"\bcc(?:\s*[:=]|\s+to)?\s*([^\n]+)", msg, re.I):
        cc.extend(_EMAIL_RE.findall(m.group(1)))
    for m in re.finditer(r"\b(?:copy|carbon\s+copy)\s+([^\n]+)", msg, re.I):
        cc.extend(_EMAIL_RE.findall(m.group(1)))
    cc.extend(default_cc_emails())
    cc = _uniq(cc, exclude=ignore_set | {from_email.lower()})

    to: list[str] = []
    for m in _TO_BLOCK_RE.finditer(msg):
        to.extend(_EMAIL_RE.findall(m.group(1)))
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


def looks_like_mission_org_discovery(user_msg: str) -> bool:
    """True only when user wants to discover mission-fit orgs (not CSR-as-sender)."""
    msg = user_msg or ""
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

    if looks_like_mission_org_discovery(msg):
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
    elif draft or send or roles.to or re.search(
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
        to_emails=roles.to,
        cc=roles.cc,
        ignore_emails=roles.ignore,
        draft=draft,
        send=send,
        reason="heuristic",
        org_limit=vol["org_limit"],
        contacts_per_org=vol["contacts_per_org"],
        search_limit=vol["search_limit"],
        email_limit=vol["email_limit"],
    )


def plan_request(
    user_msg: str,
    history: Optional[list[dict[str, str]]] = None,
) -> IntentPlan:
    """LLM + heuristic plan for which agents to invoke and mail headers."""
    base = _heuristic_plan(user_msg)
    hist = ""
    if history:
        hist = "\n".join(
            f"{m.get('role')}: {(m.get('content') or '')[:400]}" for m in history[-4:]
        )
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
    "org_limit": base.org_limit,
    "contacts_per_org": base.contacts_per_org,
    "search_limit": base.search_limit,
    "email_limit": base.email_limit,
})}

Recent chat:
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
    llm_cc: list[str] = []
    for key in ("cc", "cc_emails"):
        val = data.get(key)
        if isinstance(val, list):
            llm_cc.extend(str(x) for x in val)
        elif isinstance(val, str):
            llm_cc.extend(_EMAIL_RE.findall(val))
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
    if plan.cc:
        parts.append(f"cc: {', '.join(plan.cc)}")
    if plan.ignore_emails:
        parts.append(f"ignoring: {', '.join(plan.ignore_emails)}")
    if plan.reason:
        parts.append(f"— {plan.reason}")
    return " · ".join(parts) + "\n"
