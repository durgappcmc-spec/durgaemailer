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
_TO_EMAIL_RE = re.compile(
    r"\bto\s+(" + _EMAIL_RE.pattern + r")",
    re.I,
)
_CC_RE = re.compile(
    r"\bcc\s+(.+?)(?=\b(?:bcc|draft\s+to|ignore|attach|like\s+the|same\s+(?:style\s+)?as|modeled)\b|$)",
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
_ATTACH_RE = re.compile(
    r"\battach\s+([A-Za-z0-9._\- ]+\.[A-Za-z0-9]{1,8})",
    re.I,
)
_TEMPLATE_PATTERNS = [
    re.compile(
        r"like\s+the\s+one\s+sent\s+to\s+(" + _EMAIL_RE.pattern + r")",
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


def parse_directives(text: str) -> dict[str, Any]:
    """Parse inline draft/enrich directives from one chat message.

    Returns {to, cc, bcc, ignore, template_from, attachments, linkedin_urls}.
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

    to = ""
    m = _DRAFT_TO_RE.search(msg)
    if m:
        to = (m.group(1) or "").strip()
    else:
        # "to <email>" only if a single non-template "to" is present
        cands: list[str] = []
        for m in _TO_EMAIL_RE.finditer(msg):
            start = m.start()
            prefix = msg[max(0, start - 16) : start].lower()
            if re.search(r"sent\s+$", prefix):
                continue
            if re.search(r"email\s+$", prefix) and "draft" not in prefix:
                continue
            cands.append((m.group(1) or "").strip())
        # Drop template_from if it snuck in
        if template_from:
            cands = [c for c in cands if c.lower() != template_from.lower()]
        if len(cands) == 1:
            to = cands[0]

    cc: list[str] = []
    for m in _CC_RE.finditer(msg):
        cc.extend(_EMAIL_RE.findall(m.group(1) or ""))
    bcc: list[str] = []
    for m in _BCC_RE.finditer(msg):
        bcc.extend(_EMAIL_RE.findall(m.group(1) or ""))
    ignore = [m.group(1) for m in _IGNORE_RE.finditer(msg)]
    attachments = [m.group(1).strip() for m in _ATTACH_RE.finditer(msg)]

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

    result = {
        "to": to,
        "cc": _uniq(cc),
        "bcc": _uniq(bcc),
        "ignore": _uniq(ignore),
        "template_from": template_from,
        "attachments": attachments,
        "linkedin_urls": linkedin_urls,
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
    subject = ""
    body = ""
    try:
        raw = extract_json(
            prompt,
            system=(
                "You write CSR partnership emails. Never invent a recipient email. "
                "Never copy a style template verbatim. Return JSON only."
            ),
            max_tokens=2500,
        )
        data = json.loads(raw or "{}") if raw else {}
        if isinstance(data, dict):
            subject = str(data.get("subject") or "").strip()
            body = str(data.get("body") or data.get("html_body") or "").strip()
    except Exception as e:
        print(f"[style_draft] compose failed: {e}", file=sys.stderr)

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
    }
