# NOTE: Style profile from recent Sent mail; Gemini org_brief_synth params for synthesis.
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def build_style_profile(limit: int = 200) -> dict[str, Any]:
    samples: list[dict] = []
    try:
        from gmail_client.auth import gmail_service

        svc = gmail_service()
        res = (
            svc.users()
            .messages()
            .list(userId="me", q="in:sent", maxResults=min(limit, 200))
            .execute()
        )
        for m in res.get("messages") or []:
            full = (
                svc.users()
                .messages()
                .get(
                    userId="me",
                    id=m["id"],
                    format="metadata",
                    metadataHeaders=["Subject", "Date"],
                )
                .execute()
            )
            headers = {
                h["name"].lower(): h["value"]
                for h in (full.get("payload") or {}).get("headers") or []
            }
            samples.append(
                {
                    "id": m["id"],
                    "subject": headers.get("subject"),
                    "date": headers.get("date"),
                    "snippet": full.get("snippet") or "",
                }
            )
    except Exception as e:
        samples = [{"error": str(e)}]

    from core.agent.gemini_client import get_gemini_client

    gemini = get_gemini_client()
    prompt = (
        "From these sent-email samples, produce a style_profile JSON with keys: "
        "tone, sentence_length, greeting_style, signoff, common_phrases (list), "
        "formality (0-1), notes. Be concise.\n\n"
        f"samples={json.dumps(samples[:80], default=str)[:30000]}"
    )
    resp = gemini.generate(
        "org_brief_synth",
        prompt,
        expect_json=True,
    )
    profile = resp.parsed if isinstance(resp.parsed, dict) else {"raw": resp.text}
    profile["built_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    profile["sample_count"] = len(samples)
    try:
        from core import drive_db

        drive_db.save_style_profile(profile)
    except Exception:
        pass
    return profile
