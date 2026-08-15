# NOTE: ZI contact search by titles / seniority / domain.
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from core.tools.base import ToolContext, ToolResult


class ZoominfoSearchContactInput(BaseModel):
    org_name: str | None = None
    domain: str | None = None
    company_id: str | None = None
    titles: list[str] = Field(default_factory=list)
    seniority: list[str] = Field(default_factory=list)
    full_name: str | None = None
    limit: int = 5

    @field_validator("titles", "seniority", mode="before")
    @classmethod
    def _coerce_str_list(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            parts = [p.strip() for p in v.replace(";", ",").split(",") if p.strip()]
            return parts or ([v.strip()] if v.strip() else [])
        if isinstance(v, list):
            out: list[str] = []
            for item in v:
                if item is None:
                    continue
                s = str(item).strip()
                if s:
                    out.append(s)
            return out
        return [str(v).strip()] if str(v).strip() else []


class ZoominfoSearchContactOutput(BaseModel):
    contact: dict | None = None
    matched_on: str | None = None
    candidates: list[dict] = Field(default_factory=list)


class ZoominfoSearchContactTool:
    name = "zoominfo_search_contact"
    description = (
        "Search ZoomInfo for a persona-matched contact. "
        "Pass ranked titles one list (tried in order), plus domain or org_name."
    )
    input_schema = ZoominfoSearchContactInput
    output_schema = ZoominfoSearchContactOutput
    cost_hint = {"zi_credits": 2}
    idempotent = True
    phase_scope = {"phase1"}

    def run(self, inputs: ZoominfoSearchContactInput, ctx: ToolContext) -> ToolResult:
        from connectors.zoominfo import ZoomInfoConnector
        from core.prospect_list import has_email_or_mobile as _prospect_has_email_or_mobile
        from core import drive_db

        # Backfill persona from agent extras when planner omits titles
        persona = {}
        if isinstance(ctx.extras, dict):
            persona = ctx.extras.get("persona_target") or {}
        titles = [t for t in (inputs.titles or []) if t]
        if not titles:
            titles = [
                str(t).strip()
                for t in (persona.get("titles") or [])
                if str(t).strip()
            ]
        seniority = [s for s in (inputs.seniority or []) if s]
        if not seniority:
            seniority = [
                str(s).strip()
                for s in (persona.get("seniority") or persona.get("seniorities") or [])
                if str(s).strip()
            ]

        if not titles and not inputs.full_name:
            return ToolResult(
                ok=False,
                error="titles or full_name required (and no persona_target titles in context)",
                error_kind="invalid_input",
            )

        zi = ZoomInfoConnector()
        last_err = None
        # Keep enrich cheap: search 3, try titles in priority order
        limit = min(max(int(inputs.limit or 3), 1), 5)

        for idx, title in enumerate(titles or [None]):
            query: dict[str, Any] = {}
            if inputs.org_name:
                query["company_names"] = inputs.org_name
            if inputs.domain:
                query["company_domains"] = inputs.domain
            if inputs.company_id:
                query["company_id"] = str(inputs.company_id)
            if seniority:
                query["seniorities"] = seniority
            if title:
                query["titles"] = title
            if inputs.full_name:
                query["full_name"] = inputs.full_name

            try:
                hits = zi.search(query, limit=limit)
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
                        "org_name": inputs.org_name,
                        "domain": inputs.domain,
                        "credits": 2,
                    }
                )
            except Exception:
                pass

            if hits and isinstance(hits, list) and hits[0].get("error"):
                last_err = hits[0].get("error")
                continue
            people = [
                h
                for h in (hits or [])
                if h and not h.get("error") and _prospect_has_email_or_mobile(h)
            ]
            if not people:
                continue

            # Prefer rows that already have email, then mobile
            people = sorted(
                people,
                key=lambda p: (
                    0 if (p.get("email") or "").strip() else 1,
                    0 if (p.get("mobile") or p.get("phone") or "").strip() else 1,
                ),
            )
            best = people[0]
            contact = _normalize_contact(best)
            matched = (
                f"{title} (ZI, priority index {idx})"
                if title
                else f"name match {inputs.full_name}"
            )
            return ToolResult(
                ok=True,
                data={
                    "contact": contact,
                    "matched_on": matched,
                    "candidates": [_normalize_contact(p) for p in people[:5]],
                    "incomplete": False,
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
                [
                    row.get("firstName") or row.get("first_name"),
                    row.get("lastName") or row.get("last_name"),
                ],
            )
        ).strip()
    )
    email = row.get("email") or row.get("emailAddress") or ""
    mobile = (
        row.get("mobile")
        or row.get("phone")
        or row.get("mobilePhone")
        or row.get("mobile_phone")
        or ""
    )
    return {
        "name": name or "",
        "email": email or "",
        "mobile": mobile or "",
        "title": row.get("title") or row.get("jobTitle") or "",
        "linkedin_url": row.get("linkedin_url")
        or row.get("linkedinUrl")
        or row.get("linkedin")
        or "",
        "raw": {k: v for k, v in row.items() if k != "raw"},
    }
