# NOTE: Tool package — build_registry wires all Phase 1/2 tools.
from __future__ import annotations

from core.tools.registry import ToolRegistry


def build_registry() -> ToolRegistry:
    from core.tools.ask_human import AskHumanTool
    from core.tools.compose_hyper_personalized_email import ComposeHyperPersonalizedEmailTool
    from core.tools.find_similar_sent_email import FindSimilarSentEmailTool
    from core.tools.gmail_history_lookup import GmailHistoryLookupTool
    from core.tools.inject_tracking import InjectTrackingTool
    from core.tools.linkedin_person_search import LinkedinPersonSearchTool
    from core.tools.manual_override_contact import ManualOverrideContactTool
    from core.tools.resolve_domain import ResolveDomainTool
    from core.tools.save_draft import SaveDraftTool
    from core.tools.synthesize_org_brief import SynthesizeOrgBriefTool
    from core.tools.validate_grounding import ValidateGroundingTool
    from core.tools.web_fetch_pages import WebFetchPagesTool
    from core.tools.web_find_recent_news import WebFindRecentNewsTool
    from core.tools.web_find_team_page import WebFindTeamPageTool
    from core.tools.zoominfo_enrich_company import ZoominfoEnrichCompanyTool
    from core.tools.zoominfo_light_company_signal import ZoominfoLightCompanySignalTool
    from core.tools.zoominfo_search_contact import ZoominfoSearchContactTool

    reg = ToolRegistry()
    for tool in (
        ResolveDomainTool(),
        ZoominfoSearchContactTool(),
        ZoominfoLightCompanySignalTool(),
        WebFindTeamPageTool(),
        LinkedinPersonSearchTool(),
        ManualOverrideContactTool(),
        ZoominfoEnrichCompanyTool(),
        GmailHistoryLookupTool(),
        WebFetchPagesTool(),
        WebFindRecentNewsTool(),
        SynthesizeOrgBriefTool(),
        FindSimilarSentEmailTool(),
        ComposeHyperPersonalizedEmailTool(),
        ValidateGroundingTool(),
        InjectTrackingTool(),
        SaveDraftTool(),
        AskHumanTool(),
    ):
        reg.register(tool)

    try:
        from core import drive_db

        drive_db.save_tool_registry_manifest(reg.manifest())
    except Exception:
        pass
    return reg
