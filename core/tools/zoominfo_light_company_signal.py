# NOTE: Light firmographics for Phase 1 review grid.
from __future__ import annotations

from pydantic import BaseModel

from core.tools.base import ToolContext, ToolResult


class LightSignalInput(BaseModel):
    domain: str
    org_name: str | None = None


class LightSignalOutput(BaseModel):
    industry: str | None = None
    hq: str | None = None
    employee_band: str | None = None
    revenue_band: str | None = None


class ZoominfoLightCompanySignalTool:
    name = "zoominfo_light_company_signal"
    description = "Fetch light ZoomInfo firmographics for a domain"
    input_schema = LightSignalInput
    output_schema = LightSignalOutput
    cost_hint = {"zi_credits": 1}
    idempotent = True
    phase_scope = {"phase1"}

    def run(self, inputs: LightSignalInput, ctx: ToolContext) -> ToolResult:
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
                    "tool": "zoominfo_light_company_signal",
                    "session_id": ctx.session_id,
                    "row_id": ctx.row_id,
                    "credits": 1,
                }
            )
        except Exception:
            pass
        if not hits:
            return ToolResult(
                ok=True,
                data={
                    "industry": None,
                    "hq": None,
                    "employee_band": None,
                    "revenue_band": None,
                    "source": "empty",
                },
                cost={"zi_credits": 1},
            )
        h = hits[0]
        data = {
            "industry": h.get("industry") or h.get("primaryIndustry") or "",
            "hq": h.get("hq")
            or h.get("city")
            or ", ".join(filter(None, [h.get("city"), h.get("state"), h.get("country")])),
            "employee_band": str(
                h.get("employee_band")
                or h.get("employeeCount")
                or h.get("employees")
                or ""
            ),
            "revenue_band": str(h.get("revenue") or h.get("revenueBand") or ""),
            "company_id": h.get("id") or h.get("companyId"),
            "org_name": h.get("name") or h.get("companyName") or inputs.org_name,
        }
        return ToolResult(ok=True, data=data, cost={"zi_credits": 1})
