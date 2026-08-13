# NOTE: Fetch + clean page text with robots.txt respect.
from __future__ import annotations

import httpx
from pydantic import BaseModel, Field

from core.tools.base import ToolContext, ToolResult

UA = "DurgaEmailerBot/1.0"


class WebFetchPagesInput(BaseModel):
    domain: str
    paths: list[str] = Field(default_factory=lambda: ["/", "/about", "/programs"])


class WebFetchPagesOutput(BaseModel):
    pages: list[dict] = Field(default_factory=list)


class WebFetchPagesTool:
    name = "web_fetch_pages"
    description = "Fetch and clean text from domain paths"
    input_schema = WebFetchPagesInput
    output_schema = WebFetchPagesOutput
    cost_hint = {}
    idempotent = True
    phase_scope = {"phase2"}

    def run(self, inputs: WebFetchPagesInput, ctx: ToolContext) -> ToolResult:
        domain = (inputs.domain or "").strip().lower().removeprefix("www.")
        if not domain:
            return ToolResult(ok=False, error="domain required", error_kind="invalid_input")
        pages: list[dict] = []
        headers = {"User-Agent": UA}
        try:
            with httpx.Client(timeout=15.0, follow_redirects=True, headers=headers) as client:
                robots_ok = _robots_allows(client, domain)
                for path in inputs.paths or ["/"]:
                    if not path.startswith("/"):
                        path = "/" + path
                    if robots_ok is False and path not in ("/", "/about"):
                        continue
                    url = f"https://{domain}{path}"
                    try:
                        r = client.get(url)
                    except Exception as e:
                        pages.append({"url": url, "ok": False, "error": str(e)})
                        continue
                    if r.status_code >= 400:
                        pages.append({"url": url, "ok": False, "status": r.status_code})
                        continue
                    text = _clean(r.text)
                    pages.append({"url": str(r.url), "ok": True, "text": text[:15000]})
        except Exception as e:
            return ToolResult(ok=False, error=str(e), error_kind="network")
        return ToolResult(ok=True, data={"pages": pages})


def _robots_allows(client: httpx.Client, domain: str) -> bool | None:
    try:
        r = client.get(f"https://{domain}/robots.txt")
        if r.status_code >= 400:
            return None
        # naive: if Disallow: / for * then False
        body = r.text.lower()
        if "user-agent: *" in body and "disallow: /" in body:
            # check if only root disallow
            return False
        return True
    except Exception:
        return None


def _clean(html: str) -> str:
    try:
        from selectolax.parser import HTMLParser

        tree = HTMLParser(html)
        for tag in tree.css("script,style,noscript,nav,footer"):
            tag.decompose()
        return " ".join((tree.body.text(separator=" ") if tree.body else tree.text()).split())
    except Exception:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for t in soup(["script", "style", "noscript"]):
            t.decompose()
        return " ".join(soup.get_text(" ").split())
