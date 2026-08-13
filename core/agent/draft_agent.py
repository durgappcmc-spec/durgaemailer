# NOTE: Phase 2 DraftAgent loop.
from __future__ import annotations

from core.agent.planner_base import DraftAgentBudget, PlannerBase, RowState
from core.agent.gemini_client import GeminiClient
from core.tools.registry import ToolRegistry


class DraftAgent(PlannerBase):
    phase = "phase2"
    task_kind = "draft_planner"
    prompt_file = "draft_planner.md"
    budget = DraftAgentBudget()

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        gemini: GeminiClient | None = None,
        budget: DraftAgentBudget | None = None,
        job_id: str | None = None,
        persist_row=None,
        phase2_config: dict | None = None,
    ) -> None:
        super().__init__(
            registry=registry,
            gemini=gemini,
            budget=budget or DraftAgentBudget(),
            job_id=job_id,
            persist_row=persist_row,
        )
        self.phase2_config = phase2_config or {}

    def run(self, row: RowState, *, extras: dict | None = None) -> RowState:
        extras = dict(extras or {})
        extras.setdefault("phase2_config", self.phase2_config)
        extras.setdefault("intent", self.phase2_config.get("intent", "partnership_outreach"))
        extras.setdefault("instructions", self.phase2_config.get("instructions", ""))
        extras.setdefault("source_email", self.phase2_config.get("source_email"))
        extras.setdefault("style_profile", self.phase2_config.get("style_profile"))
        return super().run(row, extras=extras)
