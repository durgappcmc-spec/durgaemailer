# NOTE: Ground answers and drafts in chat/session facts to reduce hallucination.
from __future__ import annotations

import re
from typing import Any, Optional

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _trim(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def extract_chat_facts(
    history: Optional[list[dict[str, str]]] = None,
    *,
    max_turns: int = 20,
) -> dict[str, Any]:
    """Pull concrete facts from recent chat (emails, companies, draft targets)."""
    emails: list[str] = []
    draft_tos: list[str] = []
    companies: list[str] = []
    like_refs: list[str] = []
    subjects: list[str] = []
    message_ids: list[str] = []

    for m in (history or [])[-max_turns:]:
        content = str(m.get("content") or "")
        role = (m.get("role") or "").lower()
        if not content:
            continue

        for em in _EMAIL_RE.findall(content):
            emails.append(em)

        for em in re.findall(
            r"(?:Draft|Sent|Drafted)\s*→\s*"
            r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})",
            content,
            re.I,
        ):
            draft_tos.append(em)

        for mid in re.findall(
            r"(?:message[_ -]?id|id)\s*[:=`]+\s*([A-Za-z0-9_\-]{10,})",
            content,
            re.I,
        ):
            if "@" not in mid:
                message_ids.append(mid)

        for m_like in re.finditer(
            r"like sent to:\s*([^\n→]+)|"
            r"finding Gmail sent to\s+\*\*([^*]+)\*\*|"
            r"Template recipient:\s*`([^`]+)`|"
            r"like\s+([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})",
            content,
            re.I,
        ):
            ref = next((g for g in m_like.groups() if g), "")
            if ref:
                like_refs.append(ref.strip())

        for m_co in re.finditer(
            r"(?:for|company|org(?:anisation|anization)?)\s+"
            r"([A-Za-z0-9][A-Za-z0-9&.\'\- ]{1,40})",
            content,
            re.I,
        ):
            cand = m_co.group(1).strip(" .,;:")
            if cand.lower() not in (
                "chat",
                "review",
                "csr",
                "email",
                "draft",
                "me",
                "us",
            ):
                companies.append(cand)

        if role == "assistant":
            for m_sub in re.finditer(
                r"(?:\*\*)?Subject(?:\*\*)?:\s*(.+)", content, re.I
            ):
                subjects.append(m_sub.group(1).strip()[:120])

    def _uniq(items: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for x in items:
            key = x.strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(x.strip())
        return out

    return {
        "emails": _uniq(emails)[-20:],
        "draft_tos": _uniq(draft_tos)[-10:],
        "companies": _uniq(companies)[-12:],
        "like_refs": _uniq(like_refs)[-8:],
        "subjects": _uniq(subjects)[-8:],
        "message_ids": _uniq(message_ids)[-8:],
    }


def format_session_context(
    *,
    history: Optional[list[dict[str, str]]] = None,
    prospects: Optional[list[dict[str, Any]]] = None,
    mailbox_messages: Optional[list[dict[str, Any]]] = None,
    memory_hits: Optional[list[Any]] = None,
    max_chars: int = 14000,
) -> str:
    """Build a grounding block for system prompts (chat + drafts)."""
    parts: list[str] = []
    facts = extract_chat_facts(history)

    if facts["draft_tos"]:
        parts.append("Prior draft/send To addresses in this chat:")
        parts.extend(f"- {e}" for e in facts["draft_tos"])
    if facts["like_refs"]:
        parts.append("Sent-template references discussed (NOT new To unless user says so):")
        parts.extend(f"- {r}" for r in facts["like_refs"])
    if facts["companies"]:
        parts.append("Organizations mentioned:")
        parts.extend(f"- {c}" for c in facts["companies"][:10])
    if facts["message_ids"]:
        parts.append("Gmail message ids mentioned:")
        parts.extend(f"- `{m}`" for m in facts["message_ids"])
    if facts["subjects"]:
        parts.append("Subjects seen:")
        parts.extend(f"- {s}" for s in facts["subjects"][:6])

    # Recent dialogue digest (keep more of assistant/user text than planner)
    if history:
        parts.append("Recent conversation (use these facts; do not invent):")
        for m in history[-12:]:
            role = m.get("role") or "?"
            content = _trim(str(m.get("content") or ""), 900)
            if content:
                parts.append(f"{role}: {content}")

    if prospects:
        parts.append("Loaded prospects in this session:")
        for p in prospects[:25]:
            if not isinstance(p, dict):
                continue
            name = (p.get("name") or "").strip()
            email = (p.get("email") or "").strip()
            company = (p.get("company") or "").strip()
            title = (p.get("title") or "").strip()
            line = " · ".join(x for x in (name, title, company, email) if x)
            if line:
                parts.append(f"- {line}")

    if mailbox_messages:
        parts.append("Loaded mailbox rows (inbox/sent) in this session:")
        for r in mailbox_messages[:15]:
            if not isinstance(r, dict):
                continue
            box = r.get("mailbox") or "?"
            mid = (r.get("message_id") or "").strip()
            subj = (r.get("subject") or "(no subject)")[:70]
            who = r.get("to") if box == "sent" else r.get("from")
            who = (who or "")[:50]
            id_bit = f" id=`{mid}`" if mid else ""
            parts.append(f"- [{box}]{id_bit} {who} | {subj}")

    if memory_hits:
        try:
            from core import memory as mem

            parts.append("Saved memory hits:")
            parts.append(mem.format_for_prompt(memory_hits)[:3000])
        except Exception:
            pass

    text = "\n".join(parts).strip()
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return text


def chat_grounding_system(
    *,
    history: Optional[list[dict[str, str]]] = None,
    prospects: Optional[list[dict[str, Any]]] = None,
    mailbox_messages: Optional[list[dict[str, Any]]] = None,
    memory_hits: Optional[list[Any]] = None,
    document_context: str = "",
    attachment_names: Optional[list[str]] = None,
) -> str:
    """System instruction that forces grounding in chat/session memory."""
    ctx = format_session_context(
        history=history,
        prospects=prospects,
        mailbox_messages=mailbox_messages,
        memory_hits=memory_hits,
    )
    rules = (
        "You are Relay's chat assistant for CSR outreach (Karuna Media).\n"
        "GROUNDING RULES (mandatory):\n"
        "1. Prefer facts from the conversation and session context below over "
        "general knowledge or web search.\n"
        "2. Do NOT invent recipient emails, Sent message contents, draft IDs, "
        "company names, CC aliases, or prior agreements that are not in context.\n"
        "3. If the user says 'as per chat', 'previous email', 'that company', "
        "'like that', or similar — resolve from the session context / recent "
        "conversation. If missing, ask a short clarifying question.\n"
        "4. Sent-template addresses (like-sent / 'email like info@…') are "
        "SOURCE templates, not the new To — unless the user explicitly says "
        "to email that same address.\n"
        "5. Use Google Search only for new external research; never let search "
        "override concrete chat facts (emails, orgs, drafts already discussed).\n"
        "6. When unsure, say what you know from chat and what you still need.\n"
    )
    if attachment_names:
        rules += (
            f"\nUploaded files available: {', '.join(attachment_names)}. "
            "Use extracted document text when relevant; do not invent file contents.\n"
        )
    if document_context.strip():
        rules += f"\nUploaded document text:\n{_trim(document_context, 4000)}\n"
    if ctx:
        rules += f"\n--- SESSION / CHAT CONTEXT ---\n{ctx}\n--- END CONTEXT ---\n"
    else:
        rules += (
            "\n(No prior session facts loaded yet — ask for missing details "
            "instead of guessing.)\n"
        )
    return rules


def prefers_chat_over_search(user_msg: str) -> bool:
    """True when the user is clearly referring to prior chat / session state."""
    msg = user_msg or ""
    return bool(
        re.search(
            r"\b("
            r"as per (?:the )?chat|"
            r"from (?:the )?(?:chat|history|conversation)|"
            r"previous|prior|earlier|same as|like that|that email|"
            r"we (?:discussed|said|drafted|sent|agreed)|"
            r"you (?:said|drafted|found|listed)|"
            r"remember|recall|"
            r"current (?:org|organisation|organization|company|prospect)|"
            r"last (?:draft|email|message|prospect|list)"
            r")\b",
            msg,
            re.I,
        )
    )
