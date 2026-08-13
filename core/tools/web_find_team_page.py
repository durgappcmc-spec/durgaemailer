# NOTE: Best-guess team/about/leadership page text.
from __future__ import annotations

import httpx
from pydantic import BaseModel

from core.tools.base import ToolContext, ToolResult

_PATHS = ("/team", "/about", "/about-us", "/leadership", "/our-team", "/people")


class WebFindTeamPageInput(BaseModel):
    domain: str


class WebFindTeamPageOutput(BaseModel):
    url: str | None = None
    text: str | None = None
    candidates: list[dict] = []


class WebFindTeamPageTool:
    name = "web_find_team_page"
    description = "Find team/about/leadership page text for a domain"
    input_schema = WebFindTeamPageInput
    output_schema = WebFindTeamPageOutput
    cost_hint = {}
    idempotent = True
    phase_scope = {"phase1"}

    def run(self, inputs: WebFindTeamPageInput, ctx: ToolContext) -> ToolResult:
        domain = (inputs.domain or "").strip().lower().removeprefix("www.")
        if not domain:
            return ToolResult(ok=False, error="domain required", error_kind="invalid_input")

        headers = {"User-Agent": "DurgaEmailerBot/1.0"}
        best_url = None
        best_text = ""
        try:
            with httpx.Client(timeout=15.0, follow_redirects=True, headers=headers) as client:
                for path in _PATHS:
                    url = f"https://{domain}{path}"
                    try:
                        r = client.get(url)
                    except Exception:
                        continue
                    if r.status_code >= 400:
                        continue
                    text = _clean_html(r.text)
                    if len(text) > len(best_text):
                        best_text = text
                        best_url = str(r.url)
                    if len(best_text) > 800:
                        break
        except Exception as e:
            return ToolResult(ok=False, error=str(e), error_kind="network")

        if not best_text:
            return ToolResult(ok=False, error="no team page found", error_kind="not_found")

        candidates = _extract_name_title_pairs(best_text)
        return ToolResult(
            ok=True,
            data={
                "url": best_url,
                "text": best_text[:12000],
                "candidates": candidates[:20],
            },
        )


def _clean_html(html: str) -> str:
    try:
        from selectolax.parser import HTMLParser

        tree = HTMLParser(html)
        for tag in tree.css("script,style,noscript"):
            tag.decompose()
        return " ".join((tree.body.text(separator=" ") if tree.body else tree.text()).split())
    except Exception:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for t in soup(["script", "style", "noscript"]):
            t.decompose()
        return " ".join(soup.get_text(" ").split())


def _extract_name_title_pairs(text: str) -> list[dict]:
    import re

    out: list[dict] = []
    # Heuristic: "Jane Doe, Head of CSR" / "Jane Doe – Director"
    for m in re.finditer(
        r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\s*[,–\-—:]\s*"
        r"((?:Head|Director|Manager|Chief|VP|President|Founder|CEO|COO)[^.\n]{0,60})",
        text,
    ):
        out.append({"name": m.group(1).strip(), "title": m.group(2).strip()})
    return out
