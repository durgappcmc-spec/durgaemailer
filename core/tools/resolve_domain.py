# NOTE: Org name → canonical domain (cache → ZI → tldextract/DuckDuckGo).
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from core.tools.base import ToolContext, ToolResult


class ResolveDomainInput(BaseModel):
    org_name: str
    hint_domain: str | None = None


class ResolveDomainOutput(BaseModel):
    domain: str | None = None
    org_name: str | None = None
    source: str | None = None


class ResolveDomainTool:
    name = "resolve_domain"
    description = "Resolve organisation name to a canonical domain"
    input_schema = ResolveDomainInput
    output_schema = ResolveDomainOutput
    cost_hint = {"zi_credits": 0}
    idempotent = True
    phase_scope = {"phase1"}

    def run(self, inputs: ResolveDomainInput, ctx: ToolContext) -> ToolResult:
        from core import drive_db

        name = (inputs.org_name or "").strip()
        if not name:
            return ToolResult(ok=False, error="org_name required", error_kind="invalid_input")

        if inputs.hint_domain:
            domain = _normalize_domain(inputs.hint_domain)
            if domain:
                drive_db.set_cached_domain(name, domain, name)
                return ToolResult(
                    ok=True,
                    data={"domain": domain, "org_name": name, "source": "hint"},
                )

        cached = drive_db.get_cached_domain(name)
        if cached:
            return ToolResult(
                ok=True,
                data={"domain": cached, "org_name": name, "source": "cache"},
            )

        # ZoomInfo company search
        try:
            from connectors.zoominfo import ZoomInfoConnector

            zi = ZoomInfoConnector()
            hits = zi._search_companies(  # noqa: SLF001 — intentional reuse
                {"company_names": name, "keywords": name}, limit=3
            )
            for h in hits or []:
                domain = _normalize_domain(
                    h.get("domain") or h.get("website") or h.get("companyWebsite") or ""
                )
                org = h.get("name") or h.get("companyName") or name
                if domain:
                    drive_db.set_cached_domain(name, domain, org)
                    try:
                        drive_db.log_zoominfo_call(
                            {
                                "tool": "resolve_domain",
                                "session_id": ctx.session_id,
                                "row_id": ctx.row_id,
                                "credits": 1,
                            }
                        )
                    except Exception:
                        pass
                    return ToolResult(
                        ok=True,
                        data={"domain": domain, "org_name": org, "source": "zoominfo"},
                        cost={"zi_credits": 1},
                    )
        except Exception as e:
            zi_err = str(e)
        else:
            zi_err = None

        # DuckDuckGo HTML (best-effort)
        domain = _ddg_domain(name)
        if domain:
            drive_db.set_cached_domain(name, domain, name)
            return ToolResult(
                ok=True,
                data={"domain": domain, "org_name": name, "source": "web"},
            )

        # tldextract on name-as-domain guess
        try:
            import tldextract

            ext = tldextract.extract(name.replace(" ", "").lower() + ".org")
            if ext.domain and ext.suffix:
                guess = f"{ext.domain}.{ext.suffix}"
                return ToolResult(
                    ok=True,
                    data={"domain": guess, "org_name": name, "source": "guess"},
                )
        except Exception:
            pass

        return ToolResult(
            ok=False,
            error=zi_err or f"could not resolve domain for {name}",
            error_kind="not_found",
        )


def _normalize_domain(raw: str) -> str | None:
    s = (raw or "").strip().lower()
    if not s:
        return None
    if "://" not in s:
        s = "https://" + s
    try:
        host = urlparse(s).hostname or ""
    except Exception:
        host = ""
    host = host.removeprefix("www.")
    if not host or "." not in host:
        return None
    return host


def _ddg_domain(org_name: str) -> str | None:
    try:
        q = f"{org_name} official website"
        with httpx.Client(timeout=12.0, follow_redirects=True) as client:
            r = client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": q},
                headers={"User-Agent": "DurgaEmailerBot/1.0"},
            )
            if r.status_code >= 400:
                return None
            text = r.text
        for m in re.finditer(r'uddg=([^&"]+)', text):
            from urllib.parse import unquote

            url = unquote(m.group(1))
            d = _normalize_domain(url)
            if d and not any(
                x in d
                for x in (
                    "duckduckgo.",
                    "wikipedia.",
                    "linkedin.",
                    "facebook.",
                    "twitter.",
                    "youtube.",
                )
            ):
                return d
        for m in re.finditer(r'https?://([a-z0-9.-]+\.[a-z]{2,})', text, re.I):
            d = _normalize_domain(m.group(0))
            if d and "duckduckgo" not in d:
                return d
    except Exception:
        return None
    return None
