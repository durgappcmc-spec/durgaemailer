# NOTE: ZI contact search by titles / seniority / domain.
from __future__ import annotations

from pydantic import BaseModel, Field

from core.tools.base import ToolContext, ToolResult


class ZoominfoSearchContactInput(BaseModel):
    org_name: str | None = None
    domain: str | None = None
    company_id: str | None = None
    titles: list[str] = Field(default_factory=list)
    seniority: list[str] = Field(default_factory=list)
    full_name: str | None = None
    limit: int = 5


class ZoominfoSearchContactOutput(BaseModel):
    contact: dict | None = None
    matched_on: str | None = None
    candidates: list[dict] = Field(default_factory=list)


class ZoominfoSearchContactTool:
    name = "zoominfo_search_contact"
    description = "Search ZoomInfo for a persona-matched contact"
    input_schema = ZoominfoSearchContactInput
    output_schema = ZoominfoSearchContactOutput
    cost_hint = {"zi_credits": 2}
    idempotent = True
    phase_scope = {"phase1"}

    def run(self, inputs: ZoominfoSearchContactInput, ctx: ToolContext) -> ToolResult:
        from connectors.zoominfo import ZoomInfoConnector
        from core import drive_db

        titles = [t for t in (inputs.titles or []) if t]
        if not titles and not inputs.full_name:
            return ToolResult(
                ok=False,
                error="titles or full_name required",
                error_kind="invalid_input",
            )

        zi = ZoomInfoConnector()
        last_err = None
        for idx, title in enumerate(titles or [None]):
            query: dict = {"limit": inputs.limit}
            if inputs.org_name:
                query["company_names"] = inputs.org_name
            if inputs.domain:
                query["company_domains"] = inputs.domain
            if inputs.seniority:
                query["seniorities"] = inputs.seniority
            if title:
                query["titles"] = title
            if inputs.full_name:
                query["full_name"] = inputs.full_name
            try:
                hits = zi.search(query, limit=inputs.limit)
            except Exception as e:
                last_err = str(e)
                continue
            try:
                drive_db.log_zoominfo_call(
                    {
                        "tool": "zoominfo_search_contact",
                        "session_id": ctx.session_id,
                        "row_id": ctx.row_id,
                        "title": title,
                        "credits": 2,
                    }
                )
            except Exception:
                pass
            if hits and isinstance(hits, list) and hits[0].get("error"):
                last_err = hits[0].get("error")
                continue
            people = [h for h in (hits or []) if h and not h.get("error")]
            if not people:
                continue
            best = people[0]
            contact = _normalize_contact(best)
            matched = (
                f"{title} (ZI, priority index {idx})"
                if title
                else f"name match {inputs.full_name}"
            )
            if not contact.get("email") and not contact.get("mobile"):
                # still return but mark incomplete
                return ToolResult(
                    ok=True,
                    data={
                        "contact": contact,
                        "matched_on": matched,
                        "candidates": [_normalize_contact(p) for p in people[:5]],
                        "incomplete": True,
                    },
                    cost={"zi_credits": 2},
                )
            return ToolResult(
                ok=True,
                data={
                    "contact": contact,
                    "matched_on": matched,
                    "candidates": [_normalize_contact(p) for p in people[:5]],
                },
                cost={"zi_credits": 2},
            )

        return ToolResult(
            ok=False,
            error=last_err or "no contact found for title priority",
            error_kind="not_found",
            cost={"zi_credits": 2},
        )


def _normalize_contact(row: dict) -> dict:
    name = (
        row.get("name")
        or row.get("fullName")
        or " ".join(
            filter(
                None,
                [row.get("firstName") or row.get("first_name"), row.get("lastName") or row.get("last_name")],
            )
        ).strip()
    )
    return {
        "name": name or "",
        "email": row.get("email") or row.get("emailAddress") or "",
        "mobile": row.get("mobile") or row.get("phone") or row.get("mobilePhone") or "",
        "title": row.get("title") or row.get("jobTitle") or "",
        "linkedin_url": row.get("linkedin_url") or row.get("linkedinUrl") or row.get("linkedin") or "",
        "raw": {k: v for k, v in row.items() if k != "raw"},
    }
