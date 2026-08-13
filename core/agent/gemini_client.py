# NOTE: Thin task_kind wrapper over core.llm — single model from settings.GEMINI_MODEL.
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from core import llm as llm_mod

_PARAMS_PATH = Path(__file__).resolve().parents[2] / "gemini_params.yaml"
_params_mtime: float = 0.0
_params_cache: dict[str, Any] = {}


def _is_rate_limit(exc: BaseException) -> bool:
    msg = str(exc).lower()
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if code in (429, "429"):
        return True
    return "429" in msg or ("rate" in msg and "limit" in msg) or "resource_exhausted" in msg


@dataclass
class GeminiResponse:
    text: str
    task_kind: str
    tokens_in: int = 0
    tokens_out: int = 0
    wall_ms: int = 0
    parsed: Any = None
    repaired: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


class RateLimitedError(Exception):
    """Propagated so planners can return {action: wait}."""


def _load_params(force: bool = False) -> dict[str, Any]:
    global _params_mtime, _params_cache
    path = _PARAMS_PATH
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {
            "defaults": {
                "temperature": 0.2,
                "max_output_tokens": 2048,
                "response_mime_type": "application/json",
            },
            "task_kinds": {},
        }
    if not force and _params_cache and mtime == _params_mtime:
        return _params_cache
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    _params_cache = data
    _params_mtime = mtime
    return data


def params_for(task_kind: str) -> dict[str, Any]:
    cfg = _load_params()
    defaults = dict(cfg.get("defaults") or {})
    kind = dict((cfg.get("task_kinds") or {}).get(task_kind) or {})
    return {**defaults, **kind}


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


class GeminiClient:
    """Single-model client; model ID comes only from core.llm / settings.GEMINI_MODEL."""

    def __init__(self, existing_wrapper: Any = None) -> None:
        self._llm = existing_wrapper or llm_mod

    def generate(
        self,
        task_kind: str,
        prompt: str,
        *,
        system: str | None = None,
        response_schema: dict | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        session_id: str | None = None,
        row_id: str | None = None,
        expect_json: bool = True,
    ) -> GeminiResponse:
        params = params_for(task_kind)
        temp = temperature if temperature is not None else float(params.get("temperature", 0.2))
        max_tok = (
            max_output_tokens
            if max_output_tokens is not None
            else int(params.get("max_output_tokens", 2048))
        )
        mime = params.get("response_mime_type") or (
            "application/json" if expect_json else None
        )

        t0 = time.time()
        try:
            raw = self._call_with_retry(
                prompt,
                system=system,
                temperature=temp,
                max_output_tokens=max_tok,
                response_mime_type=mime,
                response_schema=response_schema,
            )
        except Exception as e:
            if _is_rate_limit(e):
                raise RateLimitedError(str(e)) from e
            raise

        wall_ms = int((time.time() - t0) * 1000)
        text = raw.get("text") or ""
        tokens_in = int(raw.get("tokens_in") or 0)
        tokens_out = int(raw.get("tokens_out") or 0)
        parsed = None
        repaired = False

        if expect_json:
            try:
                parsed = parse_json_loose(text)
            except Exception:
                repaired_text = self._repair_json(text, system=system)
                repaired = True
                parsed = parse_json_loose(repaired_text)
                text = repaired_text

        resp = GeminiResponse(
            text=text,
            task_kind=task_kind,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            wall_ms=wall_ms,
            parsed=parsed,
            repaired=repaired,
            raw=raw,
        )
        self._log(resp, session_id=session_id, row_id=row_id)
        return resp

    def _call_with_retry(
        self,
        prompt: str,
        *,
        system: Optional[str],
        temperature: float,
        max_output_tokens: int,
        response_mime_type: Optional[str],
        response_schema: Any,
    ) -> dict[str, Any]:
        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=20),
            retry=retry_if_exception(_is_rate_limit),
            reraise=True,
        )
        def _inner() -> dict[str, Any]:
            return self._llm.generate_content_raw(
                prompt,
                system=system,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                response_mime_type=response_mime_type,
                response_schema=response_schema,
            )

        return _inner()

    def _repair_json(self, malformed: str, *, system: Optional[str]) -> str:
        repair_prompt = (
            "Return strict JSON only, no prose, no markdown fences.\n\n"
            "Malformed output to fix:\n"
            f"{malformed}"
        )
        raw = self._llm.generate_content_raw(
            repair_prompt,
            system=system,
            temperature=0.0,
            max_output_tokens=4096,
            response_mime_type="application/json",
        )
        return raw.get("text") or ""

    def _log(
        self,
        resp: GeminiResponse,
        *,
        session_id: Optional[str],
        row_id: Optional[str],
    ) -> None:
        try:
            from core import drive_db

            drive_db.log_gemini_call(
                {
                    "task_kind": resp.task_kind,
                    "tokens_in": resp.tokens_in,
                    "tokens_out": resp.tokens_out,
                    "wall_ms": resp.wall_ms,
                    "session_id": session_id,
                    "row_id": row_id,
                    "repaired": resp.repaired,
                }
            )
        except Exception:
            pass


_default_client: Optional[GeminiClient] = None


def get_gemini_client() -> GeminiClient:
    global _default_client
    if _default_client is None:
        _default_client = GeminiClient(llm_mod)
    return _default_client
