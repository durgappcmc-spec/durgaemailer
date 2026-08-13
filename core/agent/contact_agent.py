# NOTE: Phase 1 ContactAgent loop.
from __future__ import annotations

from typing import Any, Optional

from core.agent.planner_base import ContactAgentBudget, PlannerBase, RowState
from core.agent.gemini_client import GeminiClient
from core.tools.registry import ToolRegistry


class ContactAgent(PlannerBase):
    phase = "phase1"
    task_kind = "contact_planner"
    prompt_file = "contact_planner.md"
    budget = ContactAgentBudget()

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        gemini: GeminiClient | None = None,
        budget: ContactAgentBudget | None = None,
        job_id: str | None = None,
        persist_row=None,
        persona_target: dict | None = None,
    ) -> None:
        super().__init__(
            registry=registry,
            gemini=gemini,
            budget=budget or ContactAgentBudget(),
            job_id=job_id,
            persist_row=persist_row,
        )
        self.persona_target = persona_target or {}

    def run(self, row: RowState, *, extras: dict | None = None) -> RowState:
        extras = dict(extras or {})
        extras.setdefault("persona_target", self.persona_target)
        return super().run(row, extras=extras)
