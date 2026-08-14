# NOTE: Genspark LLM proxy (OpenAI-compatible chat/completions) for email drafts.
from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

import httpx

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
_LLM_BASE = "https://www.genspark.ai/api/llm_proxy/v1"
_DEFAULT_MODEL = "claude-sonnet-4-6"

_REVIEW_SYSTEM = """You score whether a draft email fulfills the user's requirements.
Return JSON only:
{"ok": true/false, "score": 0.0-1.0, "issues": ["..."], "missing_requirements": ["..."]}
ok is true only if the draft clearly follows the user's request (recipient, topic,
tone, asks, constraints, attachments mentioned, style). Reject generic filler,
invented facts, ignored instructions, or a missing CTA the user asked for.
"""


def api_key() -> str:
    try:
        from config import settings

        return (getattr(settings, "GSK_API_KEY", None) or os.getenv("GSK_API_KEY") or "").strip()
    except Exception:
        return (os.getenv("GSK_API_KEY") or "").strip()


def default_model() -> str:
    try:
        from config import settings

        return (getattr(settings, "GSK_MODEL", None) or os.getenv("GSK_MODEL") or _DEFAULT_MODEL).strip()
    except Exception:
        return (os.getenv("GSK_MODEL") or _DEFAULT_MODEL).strip()


def available() -> bool:
    return bool(api_key())


def _headers() -> dict[str, str]:
    key = api_key()
    return {
        "Authorization": f"Bearer {key}",
        "X-Api-Key": key,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": _UA,
        "Origin": "https://www.genspark.ai",
        "Referer": "https://www.genspark.ai/",
    }


def chat_completions(
    prompt: str,
    *,
    system: Optional[str] = None,
    model: Optional[str] = None,
    temperature: float = 0.3,
    max_tokens: int = 2500,
    json_mode: bool = False,
    timeout: float = 90.0,
) -> dict[str, Any]:
    """POST /chat/completions. Returns {text, tokens_in, tokens_out, model}."""
    if not api_key():
        raise RuntimeError("GSK_API_KEY is not set")
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload: dict[str, Any] = {
        "model": model or default_model(),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.post(
            f"{_LLM_BASE}/chat/completions",
            headers=_headers(),
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
    choice = ((data.get("choices") or [{}])[0].get("message") or {})
    text = str(choice.get("content") or "").strip()
    usage = data.get("usage") or {}
    return {
        "text": text,
        "tokens_in": int(usage.get("prompt_tokens") or 0),
        "tokens_out": int(usage.get("completion_tokens") or 0),
        "model": data.get("model") or payload["model"],
        "raw": data,
    }


def parse_json_loose(text: str) -> Any:
    raw = (text or "").strip()
    if not raw:
        raise json.JSONDecodeError("empty", raw, 0)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    if fence:
        return json.loads(fence.group(1).strip())
    for opener, closer in (("{", "}"), ("[", "]")):
        start = raw.find(opener)
        end = raw.rfind(closer)
        if start >= 0 and end > start:
            return json.loads(raw[start : end + 1])
    raise json.JSONDecodeError("no json", raw, 0)


def compose_json(
    prompt: str,
    *,
    system: Optional[str] = None,
    max_tokens: int = 2500,
    temperature: float = 0.3,
) -> dict[str, Any]:
    raw = chat_completions(
        prompt,
        system=system,
        max_tokens=max_tokens,
        temperature=temperature,
        json_mode=True,
    )
    parsed = parse_json_loose(raw.get("text") or "")
    if not isinstance(parsed, dict):
        parsed = {"value": parsed}
    parsed["_genspark"] = {
        "model": raw.get("model"),
        "tokens_in": raw.get("tokens_in"),
        "tokens_out": raw.get("tokens_out"),
    }
    return parsed


def review_email(
    *,
    user_msg: str,
    subject: str,
    body: str,
    to_email: str = "",
) -> dict[str, Any]:
    """Score a draft against the user's request. Never raises."""
    fallback = {
        "ok": True,
        "score": 0.7,
        "issues": [],
        "missing_requirements": [],
    }
    if not available() or not (body or "").strip():
        return fallback
    prompt = (
        f"USER REQUIREMENT:\n{(user_msg or '')[:6000]}\n\n"
        f"TO: {to_email}\n"
        f"SUBJECT: {subject}\n"
        f"BODY:\n{(body or '')[:8000]}\n"
    )
    try:
        data = compose_json(
            prompt,
            system=_REVIEW_SYSTEM,
            max_tokens=800,
            temperature=0.0,
        )
    except Exception as e:
        print(f"[genspark] review failed: {e}", flush=True)
        return fallback
    score = data.get("score")
    try:
        score_f = float(score)
    except (TypeError, ValueError):
        score_f = 0.5
    ok = bool(data.get("ok")) and score_f >= 0.6
    issues = data.get("issues") if isinstance(data.get("issues"), list) else []
    missing = (
        data.get("missing_requirements")
        if isinstance(data.get("missing_requirements"), list)
        else []
    )
    return {
        "ok": ok,
        "score": score_f,
        "issues": [str(x) for x in issues if x][:8],
        "missing_requirements": [str(x) for x in missing if x][:8],
    }


def as_task_client():
    """Drop-in for GeminiClient.generate() used by hyper_drafter."""
    from core.agent.gemini_client import GeminiResponse

    class _Client:
        def generate(
            self,
            task_kind: str,
            prompt: str,
            *,
            system: str | None = None,
            expect_json: bool = True,
            **_kwargs: Any,
        ) -> GeminiResponse:
            raw = chat_completions(
                prompt,
                system=system,
                json_mode=expect_json,
                max_tokens=4096,
                temperature=0.3 if task_kind == "compose_email" else 0.1,
            )
            text = raw.get("text") or ""
            parsed = None
            repaired = False
            if expect_json:
                try:
                    parsed = parse_json_loose(text)
                except Exception:
                    repaired = True
                    fix = chat_completions(
                        "Return strict JSON only, no prose.\n\n" + text,
                        system=system,
                        json_mode=True,
                        temperature=0.0,
                        max_tokens=4096,
                    )
                    text = fix.get("text") or text
                    parsed = parse_json_loose(text)
            return GeminiResponse(
                text=text,
                task_kind=task_kind,
                tokens_in=int(raw.get("tokens_in") or 0),
                tokens_out=int(raw.get("tokens_out") or 0),
                parsed=parsed,
                repaired=repaired,
                raw=raw,
            )

    return _Client()
