# NOTE: Parse inline draft directives + compose a single styled Gmail draft.
from __future__ import annotations

import html as _html
import json
import re
import sys
from typing import Any, Optional

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

_DRAFT_TO_RE = re.compile(
    r"\bdraft\s+to\s+(" + _EMAIL_RE.pattern + r")",
    re.I,
)
_SEND_TO_RE = re.compile(
    r"\b(?:send|sent)\s+to\s+(" + _EMAIL_RE.pattern + r")",
    re.I,
)
_EMAIL_ADDR_RE = re.compile(
    r"\bemail\s+(" + _EMAIL_RE.pattern + r")",
    re.I,
)
_DRAFT_FOR_RE = re.compile(
    r"\bdraft\s+for\s+(" + _EMAIL_RE.pattern + r")",
    re.I,
)
_EMAIL_LIST_RE = (
    r"("
    + _EMAIL_RE.pattern
    + r"(?:\s*(?:[,;]|\band\b)\s*(?:and\s+)?"
    + _EMAIL_RE.pattern
    + r")*)"
)
_DEST_TO_LIST_RE = re.compile(
    r"\b(?:draft|create|compose|write|make|send|sent)\s+"
    r"(?:an?\s+)?(?:email\s+|mail\s+)?"
    r"to\s+"
    + _EMAIL_LIST_RE,
    re.I,
)
_TO_EMAIL_LIST_RE = re.compile(
    r"\bto\s+" + _EMAIL_LIST_RE,
    re.I,
)
_BULK_KEYWORDS = (
    "draft to all",
    "draft for all",
    "draft to everyone",
    "bulk draft",
    "draft to the list",
    "draft to prospects",
    "draft to all prospects",
    "send to all",
    "email all",
    "email everyone",
    "draft to each",
    "draft to every prospect",
)
_CC_RE = re.compile(
    r"\bcc\s+(.+?)(?=\b(?:bcc|draft\s+to|draft\s+email|ignore|attach|"
    r"like\s+(?:the|sent)|same\s+(?:style\s+)?as|modeled)\b|$)",
    re.I | re.S,
)
_BCC_RE = re.compile(
    r"\bbcc\s+(.+?)(?=\b(?:cc|draft\s+to|ignore|attach|like\s+the|same\s+(?:style\s+)?as)\b|$)",
    re.I | re.S,
)
_IGNORE_RE = re.compile(
    r"\bignore\s+(" + _EMAIL_RE.pattern + r")",
    re.I,
)
_ATTACH_FILE = (
    r"[A-Za-z0-9._\-]+(?:[ ]+[A-Za-z0-9._\-]+)*\.[A-Za-z0-9]{1,8}"
)
_ATTACH_STEM = (
    r"[A-Za-z0-9_\-]{3,}(?:[ ]+[A-Za-z0-9_\-]{2,}){0,4}"
)
_ATTACH_RE = re.compile(
    r"\battach\s+(?:the\s+|file\s+)?(" + _ATTACH_FILE + r")",
    re.I,
)
# Stem without extension: "attach the one-pager"
_ATTACH_STEM_RE = re.compile(
    r"\battach\s+(?:the\s+|file\s+)?(" + _ATTACH_STEM + r")"
    r"(?=\s+(?:to\s+|for\s+|and\b|cc\b|bcc\b|ignore\b|$)|[,;])",
    re.I,
)
_ATTACH_TO_EMAIL_RE = re.compile(
    r"\battach\s+(?:the\s+|file\s+)?"
    r"(" + _ATTACH_FILE + r"|" + _ATTACH_STEM + r")"
    r"\s+(?:to|for)\s+(" + _EMAIL_RE.pattern + r")",
    re.I,
)
_USE_AS_ATTACH_RE = re.compile(
    r"\b(?:use|using|include)\s+(?:the\s+)?"
    r"(" + _ATTACH_FILE + r")\s+as\s+(?:an?\s+)?attachment",
    re.I,
)
_TEMPLATE_TO_PREFIX_RE = re.compile(
    r"(?:like|similar|style|modeled|sent)(?:\s+(?:the\s+)?(?:one|email|mail))?(?:\s+sent)?\s+$",
    re.I,
)
_TEMPLATE_PATTERNS = [
    re.compile(
        r"like\s+the\s+one\s+sent\s+to\s+(" + _EMAIL_RE.pattern + r")",
        re.I,
    ),
    re.compile(
        r"like\s+(?:the\s+)?(?:email|mail)\s+sent\s+to\s+("
        + _EMAIL_RE.pattern
        + r")",
        re.I,
    ),
    re.compile(
        r"like\s+(?:the\s+)?(?:email|mail|one)\s+"
        r"(?:that\s+(?:was\s+)?)?(?:we\s+|you\s+|i\s+)?"
        r"sent\s+to\s+(" + _EMAIL_RE.pattern + r")",
        re.I,
    ),
    # "draft email like sent to gargi@…" (no "the one" / "email" between like and sent)
    re.compile(
        r"(?:like|similar(?:\s+to)?)\s+(?:the\s+)?(?:one\s+)?"
        r"(?:that\s+(?:was\s+)?)?(?:we\s+|you\s+|i\s+)?"
        r"sent\s+to\s+(" + _EMAIL_RE.pattern + r")",
        re.I,
    ),
    re.compile(
        r"same\s+style\s+as\s+sent\s+to\s+(" + _EMAIL_RE.pattern + r")",
        re.I,
    ),
    re.compile(
        r"same\s+as\s+sent\s+to\s+(" + _EMAIL_RE.pattern + r")",
        re.I,
    ),
    re.compile(
        r"modeled\s+on\s+the\s+email\s+to\s+(" + _EMAIL_RE.pattern + r")",
        re.I,
    ),
]

_PROSE_RULES = (
    "Write the email body as normal prose. Do NOT insert manual line "
    "breaks inside paragraphs. Separate paragraphs with exactly one "
    "blank line. Do not indent. Use single spaces between words and "
    "single spaces after punctuation. No trailing spaces."
)
_MD_RULE = (
    "Do not use markdown (no **bold**, no bullet dashes) unless the "
    "style template uses them."
)


def looks_like_bulk_request(text: str) -> bool:
    """True only when the user explicitly asks to fan out (all / everyone / list)."""
    t = (text or "").lower()
    if any(k in t for k in _BULK_KEYWORDS):
        return True
    return bool(
        re.search(
            r"\b("
            r"to\s+(?:the\s+)?(?:above|above\s+(?:contacts?|list|prospects?|people|results?))|"
            r"(?:above|previous|prior|earlier|last)\s+"
            r"(?:contacts?|prospects?|people|results?|search|list)|"
            r"these\s+(?:contacts?|prospects?|people|results?)|"
            r"this\s+(?:list|search|result)|"
            r"all\s+(?:these\s+|the\s+)?(?:contacts?|prospects?)|"
            r"everyone\s+(?:we\s+|you\s+)?found|"
            r"zoominfo\s+list|"
            r"all\s+of\s+them"
            r")\b",
            t,
            re.I,
        )
    )


def directive_to_list(directives: dict[str, Any]) -> list[str]:
    """Explicit To addresses from parse_directives (string or list)."""
    raw = directives.get("to_list")
    if isinstance(raw, list) and raw:
        items = raw
    else:
        to = directives.get("to") or ""
        items = to if isinstance(to, list) else ([to] if to else [])
    out: list[str] = []
    seen: set[str] = set()
    for e in items:
        k = str(e or "").strip()
        if not k or k.lower() in seen:
            continue
        seen.add(k.lower())
        out.append(k)
    return out


def _clean_file_token(raw: str) -> str:
    token = (raw or "").strip().strip("\"'`.,;:()")
    token = re.sub(
        r"^(?:the|a|an|file|pdf|document|attachment)\s+",
        "",
        token,
        flags=re.I,
    ).strip()
    token = re.sub(
        r"\s+(?:as\s+an?\s+attachment|please|thanks|and)$",
        "",
        token,
        flags=re.I,
    ).strip()
    return token


def _is_template_to_prefix(prefix: str) -> bool:
    return bool(_TEMPLATE_TO_PREFIX_RE.search(prefix or ""))


def parse_attachment_refs(text: str) -> tuple[list[str], dict[str, list[str]]]:
    """Filenames from chat, plus optional {email: [file, ...]} assignments.

    `to jane@x.com attach a.pdf and to bob@y.com attach b.pdf` maps each PDF
    to that recipient. A bare `attach brochure.pdf` applies to every To.
    """
    msg = text or ""
    by_email: dict[str, list[str]] = {}

    def _add(email: str, filename: str) -> None:
        key = (email or "").strip().lower()
        name = _clean_file_token(filename)
        if not key or "@" not in key or not name:
            return
        cur = by_email.setdefault(key, [])
        if name.lower() not in {x.lower() for x in cur}:
            cur.append(name)

    for m in _ATTACH_TO_EMAIL_RE.finditer(msg):
        _add(m.group(2), m.group(1))

    def _files_in(text: str) -> list[str]:
        found: list[str] = []
        seen_l: set[str] = set()
        for rx in (_ATTACH_RE, _USE_AS_ATTACH_RE):
            for m in rx.finditer(text or ""):
                name = _clean_file_token(m.group(1))
                key = name.lower()
                if name and key not in seen_l:
                    seen_l.add(key)
                    found.append(name)
        if not found:
            for m in _ATTACH_STEM_RE.finditer(text or ""):
                name = _clean_file_token(m.group(1))
                if not name or "." in name:
                    continue
                key = name.lower()
                if key in seen_l or key in {
                    "the",
                    "file",
                    "pdf",
                    "document",
                    "attached",
                }:
                    continue
                seen_l.add(key)
                found.append(name)
        return found

    clauses: list[tuple[list[str], str]] = []
    matches = [
        m
        for m in _TO_EMAIL_LIST_RE.finditer(msg)
        if not _is_template_to_prefix(msg[max(0, m.start() - 40) : m.start()])
    ]
    if matches:
        preamble = msg[: matches[0].start()]
        if preamble.strip():
            clauses.append(([], preamble))
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(msg)
            emails = _EMAIL_RE.findall(m.group(1) or "")
            clauses.append((emails, msg[m.start() : end]))
    else:
        clauses.append(([], msg))

    assigned_names: set[str] = set()
    for emails, clause in clauses:
        files = _files_in(clause)
        if emails and files:
            for e in emails:
                for f in files:
                    _add(e, f)
                    assigned_names.add(f.lower())

    global_names: list[str] = []
    seen: set[str] = set()
    for name in _files_in(msg):
        key = name.lower()
        if key in assigned_names or key in seen:
            continue
        seen.add(key)
        global_names.append(name)

    all_named = list(global_names)
    for names in by_email.values():
        for n in names:
            if n.lower() not in seen:
                seen.add(n.lower())
                all_named.append(n)
    return all_named, by_email


def parse_directives(text: str) -> dict[str, Any]:
    """Parse inline draft/enrich directives from one chat message.

    Returns {to, to_list, cc, bcc, ignore, template_from, attachments,
    linkedin_urls, explicit_recipient_lock, bulk_flag}.
    `to` stays a string (first address) for existing call sites.
    """
    msg = text or ""
    linkedin_urls: list[str] = []
    try:
        from connectors.zoominfo import extract_linkedin_urls

        linkedin_urls = extract_linkedin_urls(msg)
    except Exception:
        pass

    template_from = ""
    for pat in _TEMPLATE_PATTERNS:
        m = pat.search(msg)
        if m:
            template_from = (m.group(1) or "").strip()
            break

    dest_src = msg
    for pat in _TEMPLATE_PATTERNS:
        dest_src = pat.sub(" ", dest_src)

    to_specific: list[str] = []
    for pat in (_DEST_TO_LIST_RE, _DRAFT_TO_RE, _SEND_TO_RE, _EMAIL_ADDR_RE, _DRAFT_FOR_RE):
        for m in pat.finditer(dest_src):
            prefix = dest_src[max(0, m.start() - 40) : m.start()].lower()
            if re.search(
                r"(?:like|similar|style|modeled)(?:\s+(?:the\s+)?(?:one|email|mail))?(?:\s+sent)?\s+$",
                prefix,
            ):
                continue
            chunk = (m.group(1) or "").strip()
            if chunk:
                to_specific.extend(_EMAIL_RE.findall(chunk) or [chunk])
    to_generic: list[str] = []
    for m in _TO_EMAIL_LIST_RE.finditer(dest_src):
        start = m.start()
        prefix = dest_src[max(0, start - 40) : start].lower()
        if re.search(
            r"(?:like|similar|style|modeled)(?:\s+(?:the\s+)?(?:one|email|mail))?(?:\s+sent)?\s+$",
            prefix,
        ):
            continue
        chunk = (m.group(1) or "").strip()
        if chunk:
            to_generic.extend(_EMAIL_RE.findall(chunk) or [chunk])
    to_list = to_specific + to_generic

    def _uniq(items: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for e in items:
            k = (e or "").strip().lower()
            if not k or k in seen:
                continue
            seen.add(k)
            out.append(e.strip())
        return out

    to_final = _uniq(to_list)
    explicit_set = {e.lower() for e in _uniq(to_specific)}

    def _drop_accidental_template(addr: str, current: list[str]) -> list[str]:
        key = (addr or "").strip().lower()
        if not key or key in explicit_set:
            return current
        others = [e for e in current if e.lower() != key]
        return others if others else current

    if template_from:
        to_final = _drop_accidental_template(template_from, to_final)
    try:
        from agent.intent import parse_like_sent_request

        like = parse_like_sent_request(msg) or {}
        like_ref = str(like.get("reference") or "").strip().lower()
        if like_ref and "@" in like_ref:
            if not template_from:
                template_from = like.get("reference") or ""
            to_final = _drop_accidental_template(like_ref, to_final)
    except Exception:
        pass
    to = to_final[0] if to_final else ""

    cc: list[str] = []
    for m in _CC_RE.finditer(msg):
        span = m.group(1) or ""
        cc.extend(_EMAIL_RE.findall(span))
        try:
            from agent.contact_aliases import resolve_names_in_text

            cc.extend(resolve_names_in_text(span))
        except Exception:
            pass
    bcc: list[str] = []
    for m in _BCC_RE.finditer(msg):
        bcc.extend(_EMAIL_RE.findall(m.group(1) or ""))
    ignore = [m.group(1) for m in _IGNORE_RE.finditer(msg)]
    attachments, attachments_by_email = parse_attachment_refs(msg)

    result = {
        "to": to,
        "to_list": to_final,
        "cc": _uniq(cc),
        "bcc": _uniq(bcc),
        "ignore": _uniq(ignore),
        "template_from": template_from,
        "attachments": attachments,
        "attachments_by_email": attachments_by_email,
        "linkedin_urls": linkedin_urls,
        "explicit_recipient_lock": bool(to_final),
        "bulk_flag": looks_like_bulk_request(msg),
    }
    to_l = (to or "").lower()
    tf_l = (template_from or "").lower()
    if to_l and tf_l and to_l == tf_l:
        result["same_to_and_template_warning"] = True
    return result


def html_to_plain_fallback(html: str) -> str:
    """Strip tags and unescape entities when a sent message has no text/plain."""
    raw = html or ""
    raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
    raw = re.sub(r"</p\s*>", "\n\n", raw, flags=re.I)
    raw = re.sub(r"<[^>]+>", "", raw)
    return _html.unescape(raw)


def fetch_latest_sent_to(email: str) -> Optional[dict[str, Any]]:
    """Gmail: q='to:{email} in:sent' maxResults=1, format=full → cleaned body + structure."""
    addr = (email or "").strip()
    if not addr or "@" not in addr:
        return None
    try:
        from gmail_client.auth import gmail_service
        from gmail_client.drafts import _extract_bodies
        from gmail_client.html_format import clean_email_body, extract_style_structure

        svc = gmail_service()
        q = f"to:{addr} in:sent"
        listed = (
            svc.users()
            .messages()
            .list(userId="me", q=q, maxResults=1)
            .execute()
        )
        rows = listed.get("messages") or []
        if not rows:
            return None
        mid = rows[0].get("id")
        if not mid:
            return None
        full = (
            svc.users()
            .messages()
            .get(userId="me", id=mid, format="full")
            .execute()
        )
        payload = full.get("payload") or {}
        headers = {
            (h.get("name") or "").lower(): h.get("value") or ""
            for h in payload.get("headers") or []
        }
        html, text = _extract_bodies(payload)
        if not (text or "").strip() and html:
            text = html_to_plain_fallback(html)
        cleaned = clean_email_body(text or "")
        struct = extract_style_structure(cleaned)
        return {
            "message_id": mid,
            "subject": headers.get("subject") or "",
            "to": headers.get("to") or addr,
            "body_cleaned": cleaned,
            "body_text": text or "",
            "body_html": html or "",
            "n_paragraphs": struct["n_paragraphs"],
            "wc_per_para": struct["wc_per_para"],
            "greeting": struct["greeting"],
            "signoff": struct["signoff"],
        }
    except Exception as e:
        print(f"[style_draft] fetch sent to {addr}: {e}", file=sys.stderr)
        return None


def compose_styled_email(
    *,
    to_email: str,
    enrichment: Optional[dict[str, Any]] = None,
    style_template: Optional[dict[str, Any]] = None,
    user_msg: str = "",
    extra_instructions: str = "",
) -> dict[str, str]:
    """LLM draft matching a sent-email style; output is cleaned prose (not HTML)."""
    from core.enrich_cache import format_enrichment_fields
    from core.llm import extract_json
    from gmail_client.html_format import clean_email_body, html_from_cleaned_body

    contact_block = format_enrichment_fields(enrichment or {})
    if not contact_block:
        contact_block = f"Recipient email: {to_email}"

    style_block = ""
    constraint = ""
    allow_md = False
    if style_template and (style_template.get("body_cleaned") or "").strip():
        tmpl_from = style_template.get("to") or ""
        body = style_template["body_cleaned"]
        allow_md = bool(re.search(r"\*\*|^\s*[-•]\s", body, re.M))
        n = style_template.get("n_paragraphs") or 0
        wc = style_template.get("wc_per_para") or 0
        greeting = style_template.get("greeting") or "Hi,"
        signoff = style_template.get("signoff") or "Best regards,"
        constraint = (
            f"Match this structure exactly: {n} paragraphs, roughly {wc} "
            f"words each, greeting style: '{greeting}', sign-off: '{signoff}'."
        )
        style_block = (
            f"STYLE_TEMPLATE (from prior email sent to {tmpl_from}):\n"
            f"---\n{body.strip()}\n---\n"
            "Instructions: Match tone, register, paragraph count, approximate "
            "length per paragraph, greeting style, and sign-off. Do NOT copy "
            f"verbatim. Personalize every specific reference to the NEW "
            f"recipient ({to_email}) using the enrichment context provided.\n"
            f"{constraint}"
        )

    md_rule = (
        _MD_RULE
        if not allow_md
        else "You may use the same emphasis/list style as the template."
    )
    prompt = f"""Write ONE outreach email.

TO (new recipient): {to_email}

PROSPECT CONTEXT (labeled fields; do not invent an email):
{contact_block}

{style_block}

User request:
{user_msg}

{extra_instructions}

{_PROSE_RULES}
{md_rule}

Return JSON only:
{{"subject": "...", "body": "plain email body with paragraphs separated by blank lines"}}
"""
    system = (
        "You write CSR partnership emails. Never invent a recipient email. "
        "Never copy a style template verbatim. Follow the user requirement "
        "exactly (topic, asks, tone, constraints). Return JSON only."
    )
    subject = ""
    body = ""
    provider = "gemini"
    quality: dict[str, Any] = {
        "ok": True,
        "score": 0.7,
        "issues": [],
        "missing_requirements": [],
    }
    critique = ""
    try:
        from core.chat_llm import resolve_chat_provider

        want = resolve_chat_provider()
    except Exception:
        want = "gemini"
    for attempt in range(2 if want == "genspark" else 1):
        run_prompt = prompt
        if critique:
            run_prompt = (
                prompt
                + "\n\nPrevious draft failed a requirement check. Fix ALL of:\n"
                + critique
            )
        data: dict[str, Any] = {}
        try:
            if want == "genspark":
                from core.genspark_client import compose_json as _gsk_json

                provider = "genspark"
                data = _gsk_json(run_prompt, system=system, max_tokens=2500)
            else:
                raw = extract_json(run_prompt, system=system, max_tokens=2500)
                parsed = json.loads(raw or "{}") if raw else {}
                data = parsed if isinstance(parsed, dict) else {}
                provider = "gemini"
        except Exception as e:
            print(f"[style_draft] compose failed: {e}", file=sys.stderr)
            if provider == "genspark":
                try:
                    raw = extract_json(run_prompt, system=system, max_tokens=2500)
                    parsed = json.loads(raw or "{}") if raw else {}
                    data = parsed if isinstance(parsed, dict) else {}
                    provider = "gemini_fallback"
                except Exception as e2:
                    print(f"[style_draft] gemini fallback failed: {e2}", file=sys.stderr)
                    data = {}
        if isinstance(data, dict):
            subject = str(data.get("subject") or "").strip()
            body = str(data.get("body") or data.get("html_body") or "").strip()
        if not body or want != "genspark":
            break
        try:
            from core.genspark_client import review_email as _gsk_review

            quality = _gsk_review(
                user_msg=user_msg,
                subject=subject,
                body=body,
                to_email=to_email,
            )
        except Exception as e:
            print(f"[style_draft] quality review skipped: {e}", file=sys.stderr)
            break
        if quality.get("ok"):
            break
        bits = list(quality.get("issues") or []) + list(
            quality.get("missing_requirements") or []
        )
        critique = "\n".join(f"- {b}" for b in bits if b)
        if not critique:
            break

    if not body:
        first = str((enrichment or {}).get("first_name") or "").strip() or "there"
        body = (
            f"Hi {first},\n\n"
            "I wanted to reach out about a possible partnership.\n\n"
            "Best regards,\n"
        )
    if not subject:
        co = str((enrichment or {}).get("company") or "").strip()
        subject = f"Partnership idea{f' for {co}' if co else ''}"

    cleaned = clean_email_body(body)
    return {
        "subject": subject,
        "body_cleaned": cleaned,
        "html_body": html_from_cleaned_body(cleaned),
        "provider": provider,
        "quality_ok": bool(quality.get("ok", True)),
        "quality_score": float(quality.get("score") or 0),
        "quality_issues": list(quality.get("issues") or [])
        + list(quality.get("missing_requirements") or []),
    }
