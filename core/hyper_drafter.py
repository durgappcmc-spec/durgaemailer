# NOTE: Hyper-personalized composer + grounding validator (Gemini task_kinds).
from __future__ import annotations

import json
from typing import Any, Optional

COMPOSER_SYSTEM = """You are an expert CSR partnership email writer for Karuna Media.
Follow this grounding contract:
1. Never invent facts about the recipient organisation.
2. Every factual claim must appear in the org_brief (programs, signals, mission, HQ, industry).
3. If a needed detail is missing, use {{PLACEHOLDER:...}} — never fabricate.
4. Open with a specific, brief org-grounded hook (program or recent signal).
5. Map sender value to one flagship program or signal — explicit, not generic flattery.
6. Mirror the style_profile tone when provided; otherwise warm-professional and concise.
7. Keep body under ~220 words unless instructions say otherwise.
8. Include a single clear CTA.
9. Output strict JSON: {subject, body_html, personalization_ledger, confidence}.
10. personalization_ledger is a list of {claim, evidence_ref, source_tool} — evidence_ref must point into org_brief or source_email.
11. Greet the approved contact by first name only (e.g. Hi Sushmita,). Never put job title in parentheses. Never greet a name from source_email.
12. Never include tracking, Netlify, or click-redirect URLs in the body text or link labels.
"""


def compose_email(
    *,
    intent: str,
    contact: dict,
    org_brief: dict,
    source_email: dict | None = None,
    style_profile: dict | None = None,
    instructions: str = "",
    gemini: Any = None,
    session_id: str | None = None,
    row_id: str | None = None,
) -> tuple[dict, dict]:
    if gemini is None:
        from core.agent.gemini_client import get_gemini_client

        gemini = get_gemini_client()

    prompt = (
        f"intent={intent}\n"
        f"contact={json.dumps(contact, default=str)}\n"
        f"org_brief={json.dumps(org_brief, default=str)[:30000]}\n"
        f"source_email={json.dumps(source_email or {}, default=str)[:8000]}\n"
        f"style_profile={json.dumps(style_profile or {}, default=str)[:4000]}\n"
        f"instructions={instructions}\n"
        "Return JSON only."
    )
    resp = gemini.generate(
        "compose_email",
        prompt,
        system=COMPOSER_SYSTEM,
        session_id=session_id,
        row_id=row_id,
        expect_json=True,
    )
    draft = resp.parsed if isinstance(resp.parsed, dict) else {}
    if not draft.get("body_html") and draft.get("body"):
        draft["body_html"] = f"<p>{draft['body']}</p>"
    draft.setdefault("subject", "")
    draft.setdefault("personalization_ledger", [])
    draft.setdefault("confidence", 0.5)
    draft["to"] = contact.get("email") or ""
    draft["recipient"] = contact.get("email") or contact.get("name") or ""
    draft["recipient_name"] = contact.get("name") or ""
    name = str(contact.get("name") or "").strip()
    first = str(contact.get("first_name") or "").strip() or (
        name.split(None, 1)[0] if name else ""
    )
    title = str(contact.get("title") or contact.get("designation") or "").strip()
    if first and draft.get("body_html"):
        try:
            from gmail_client.html_format import ensure_designation_in_greeting
            from core.tracking import strip_visible_tracking_urls

            draft["body_html"] = strip_visible_tracking_urls(
                ensure_designation_in_greeting(
                    draft["body_html"], first_name=first, title=title
                )
            )
        except Exception:
            pass
    cost = {
        "gemini_tokens_in": resp.tokens_in,
        "gemini_tokens_out": resp.tokens_out,
        "gemini_task_kind": "compose_email",
        "wall_ms": resp.wall_ms,
    }
    return draft, cost


def validate_grounding(
    *,
    draft: dict,
    org_brief: dict,
    source_email: dict | None = None,
    style_profile: dict | None = None,
    gemini: Any = None,
    session_id: str | None = None,
    row_id: str | None = None,
) -> tuple[dict, dict]:
    if gemini is None:
        from core.agent.gemini_client import get_gemini_client

        gemini = get_gemini_client()

    # Heuristic: reject ledger claims that don't mention PLACEHOLDER and aren't substrings of brief
    brief_blob = json.dumps(org_brief or {}, default=str).lower()
    source_blob = json.dumps(source_email or {}, default=str).lower()
    heuristic_violations: list[dict] = []
    for entry in draft.get("personalization_ledger") or []:
        if not isinstance(entry, dict):
            continue
        claim = str(entry.get("claim") or "")
        if not claim or "PLACEHOLDER" in claim.upper():
            continue
        cl = claim.lower()
        # allow short stylistic claims
        if len(cl) < 12:
            continue
        # require some token overlap with brief/source
        tokens = [t for t in cl.replace(",", " ").split() if len(t) > 4]
        if tokens and not any(t in brief_blob or t in source_blob for t in tokens[:6]):
            heuristic_violations.append(
                {
                    "claim": claim,
                    "reason": "claim tokens not found in org_brief/source_email",
                }
            )

    prompt = (
        "Check whether every factual claim in the draft about the recipient org "
        "is supported by org_brief or source_email. Ignore {{PLACEHOLDER:...}}. "
        "Return JSON {ok: bool, violations: [{claim, reason}]}.\n\n"
        f"draft={json.dumps(draft, default=str)[:20000]}\n"
        f"org_brief={json.dumps(org_brief, default=str)[:20000]}\n"
        f"source_email={json.dumps(source_email or {}, default=str)[:5000]}\n"
    )
    resp = gemini.generate(
        "grounding_check",
        prompt,
        session_id=session_id,
        row_id=row_id,
        expect_json=True,
    )
    parsed = resp.parsed if isinstance(resp.parsed, dict) else {"ok": False, "violations": []}
    violations = list(parsed.get("violations") or []) + heuristic_violations
    # de-dupe by claim
    seen = set()
    uniq = []
    for v in violations:
        key = str(v.get("claim") if isinstance(v, dict) else v)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(v if isinstance(v, dict) else {"claim": str(v), "reason": "unspecified"})
    ok = bool(parsed.get("ok", False)) and not heuristic_violations
    if uniq:
        ok = False
    cost = {
        "gemini_tokens_in": resp.tokens_in,
        "gemini_tokens_out": resp.tokens_out,
        "gemini_task_kind": "grounding_check",
        "wall_ms": resp.wall_ms,
    }
    return {"ok": ok, "violations": uniq}, cost
