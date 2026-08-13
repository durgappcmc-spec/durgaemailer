# NOTE: Recent news/press/blog items for a domain.
from __future__ import annotations

import httpx
from pydantic import BaseModel, Field

from core.tools.base import ToolContext, ToolResult
from core.tools.web_fetch_pages import UA, _clean

_NEWS_PATHS = ("/press", "/newsroom", "/news", "/blog", "/media")


class WebFindRecentNewsInput(BaseModel):
    domain: str
    limit: int = 3


class WebFindRecentNewsOutput(BaseModel):
    items: list[dict] = Field(default_factory=list)


class WebFindRecentNewsTool:
    name = "web_find_recent_news"
    description = "Find up to 3 recent press/news/blog items"
    input_schema = WebFindRecentNewsInput
    output_schema = WebFindRecentNewsOutput
    cost_hint = {}
    idempotent = True
    phase_scope = {"phase2"}

    def run(self, inputs: WebFindRecentNewsInput, ctx: ToolContext) -> ToolResult:
        domain = (inputs.domain or "").strip().lower().removeprefix("www.")
        items: list[dict] = []
        headers = {"User-Agent": UA}
        try:
            with httpx.Client(timeout=12.0, follow_redirects=True, headers=headers) as client:
                for path in _NEWS_PATHS:
                    if len(items) >= inputs.limit:
                        break
                    url = f"https://{domain}{path}"
                    try:
                        r = client.get(url)
                    except Exception:
                        continue
                    if r.status_code >= 400:
                        continue
                    text = _clean(r.text)[:4000]
                    if len(text) < 80:
                        continue
                    # Split into rough headlines
                    lines = [ln.strip() for ln in text.split(".") if len(ln.strip()) > 40]
                    for ln in lines[: inputs.limit - len(items)]:
                        items.append({"title": ln[:160], "url": str(r.url), "snippet": ln[:400]})
                    if not lines:
                        items.append({"title": f"Update from {path}", "url": str(r.url), "snippet": text[:400]})
        except Exception as e:
            return ToolResult(ok=False, error=str(e), error_kind="network")
        return ToolResult(ok=True, data={"items": items[: inputs.limit]})
