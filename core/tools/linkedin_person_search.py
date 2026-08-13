# NOTE: Public-search LinkedIn person lookup (best-effort, no auth scrape).
from __future__ import annotations

import re
from urllib.parse import unquote

import httpx
from pydantic import BaseModel, Field

from core.tools.base import ToolContext, ToolResult


class LinkedinPersonSearchInput(BaseModel):
    org_name: str
    title_priority: list[str] = Field(default_factory=list)


class LinkedinPersonSearchOutput(BaseModel):
    name: str | None = None
    title: str | None = None
    linkedin_url: str | None = None
    snippet: str | None = None


class LinkedinPersonSearchTool:
    name = "linkedin_person_search"
    description = "Best-effort public web search for a LinkedIn person at an org"
    input_schema = LinkedinPersonSearchInput
    output_schema = LinkedinPersonSearchOutput
    cost_hint = {}
    idempotent = True
    phase_scope = {"phase1"}

    def run(self, inputs: LinkedinPersonSearchInput, ctx: ToolContext) -> ToolResult:
        title = (inputs.title_priority or ["CSR"])[0]
        q = f'site:linkedin.com/in "{inputs.org_name}" "{title}"'
        try:
            with httpx.Client(timeout=12.0, follow_redirects=True) as client:
                r = client.get(
                    "https://html.duckduckgo.com/html/",
                    params={"q": q},
                    headers={"User-Agent": "DurgaEmailerBot/1.0"},
                )
                if r.status_code >= 400:
                    return ToolResult(
                        ok=False,
                        error=f"search HTTP {r.status_code}",
                        error_kind="network",
                    )
                html = r.text
        except Exception as e:
            return ToolResult(ok=False, error=str(e), error_kind="network")

        url = None
        for m in re.finditer(r'uddg=([^&"]+)', html):
            cand = unquote(m.group(1))
            if "linkedin.com/in/" in cand:
                url = cand.split("&")[0]
                break
        if not url:
            m = re.search(r"https?://[a-z.]?linkedin\.com/in/[a-zA-Z0-9_\-%]+", html)
            if m:
                url = m.group(0)

        if not url:
            return ToolResult(ok=False, error="no LinkedIn hit", error_kind="not_found")

        # Name from slug
        slug = url.rstrip("/").split("/")[-1]
        name = slug.replace("-", " ").title()
        return ToolResult(
            ok=True,
            data={
                "name": name,
                "title": title,
                "linkedin_url": url,
                "snippet": f"Public search hit for {title} at {inputs.org_name}",
            },
        )
