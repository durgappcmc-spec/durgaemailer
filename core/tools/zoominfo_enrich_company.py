# NOTE: Full ZoomInfo company enrichment (Phase 2).
from __future__ import annotations

from pydantic import BaseModel

from core.tools.base import ToolContext, ToolResult


class EnrichCompanyInput(BaseModel):
    domain: str
    org_name: str | None = None


class EnrichCompanyOutput(BaseModel):
    company: dict | None = None


class ZoominfoEnrichCompanyTool:
    name = "zoominfo_enrich_company"
    description = "Full ZoomInfo firmographics, executives, technologies"
    input_schema = EnrichCompanyInput
    output_schema = EnrichCompanyOutput
    cost_hint = {"zi_credits": 3}
    idempotent = True
    phase_scope = {"phase2"}

    def run(self, inputs: EnrichCompanyInput, ctx: ToolContext) -> ToolResult:
        from connectors.zoominfo import ZoomInfoConnector
        from core import drive_db

        zi = ZoomInfoConnector()
        query = {"company_domains": inputs.domain}
        if inputs.org_name:
            query["company_names"] = inputs.org_name
        try:
            hits = zi._search_companies(query, limit=1)  # noqa: SLF001
        except Exception as e:
            return ToolResult(ok=False, error=str(e), error_kind="network")
        try:
            drive_db.log_zoominfo_call(
                {
                    "tool": "zoominfo_enrich_company",
                    "session_id": ctx.session_id,
                    "row_id": ctx.row_id,
                    "credits": 3,
                }
            )
        except Exception:
            pass
        if not hits:
            return ToolResult(ok=False, error="company not found", error_kind="not_found")
        h = hits[0]
        company = {
            "name": h.get("name") or h.get("companyName") or inputs.org_name,
            "domain": inputs.domain,
            "industry": h.get("industry") or h.get("primaryIndustry"),
            "hq": h.get("hq") or h.get("city"),
            "employees": h.get("employeeCount") or h.get("employees"),
            "revenue": h.get("revenue") or h.get("revenueBand"),
            "description": h.get("description") or h.get("companyDescription") or "",
            "technologies": h.get("technologies") or [],
            "executives": h.get("executives") or h.get("employeesList") or [],
            "raw": h,
        }
        return ToolResult(ok=True, data={"company": company}, cost={"zi_credits": 3})
