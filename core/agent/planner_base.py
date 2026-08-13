# NOTE: Shared agent loop — RowState, budget, trace, JSON decisions, resumability.
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from core.agent.gemini_client import GeminiClient, RateLimitedError, get_gemini_client
from core.tools.base import ToolContext
from core.tools.registry import PhaseScopeError, ToolRegistry


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ContactRecord(BaseModel):
    name: str = ""
    email: str = ""
    mobile: str = ""
    title: str = ""
    linkedin_url: str = ""
    matched_on: str = ""
    confidence: float = 0.0


class LightOrgSignal(BaseModel):
    industry: str | None = None
    hq: str | None = None
    employee_band: str | None = None
    revenue_band: str | None = None


class RowState(BaseModel):
    row_id: str
    phase: Literal["phase1", "phase2"]
    input: str
    resolved_domain: str | None = None
    resolved_org_name: str | None = None
    contact: ContactRecord | None = None
    light_org_signal: LightOrgSignal | None = None
    org_brief: dict | None = None
    org_brief_ref: str | None = None
    draft_id: str | None = None
    tracking_id: str | None = None
    draft: dict | None = None
    status: str = "queued"
    status_message: str = ""
    approved_for_phase2: bool = False
    phase1_session_id: str | None = None
    phase2_session_id: str | None = None
    zi_credits_used: int = 0
    gemini_tokens_used: int = 0
    recovery_count: int = 0
    step: int = 0
    evidence: dict[str, Any] = Field(default_factory=dict)
    awaiting_human: dict | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class AgentBudget(BaseModel):
    max_steps: int = 10
    max_wall_seconds: int = 45
    max_zi_credits: int = 20
    max_gemini_tokens: int = 50000


class ContactAgentBudget(AgentBudget):
    max_steps: int = 10
    max_wall_seconds: int = 45


class DraftAgentBudget(AgentBudget):
    max_steps: int = 20
    max_wall_seconds: int = 90


def _load_prompt(name: str) -> str:
    path = Path(__file__).parent / "prompts" / name
    return path.read_text(encoding="utf-8")


class PlannerBase:
    phase: str = "phase1"
    task_kind: str = "contact_planner"
    prompt_file: str = "contact_planner.md"
    budget: AgentBudget = AgentBudget()

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        gemini: GeminiClient | None = None,
        budget: AgentBudget | None = None,
        job_id: str | None = None,
        persist_row=None,
    ) -> None:
        self.registry = registry
        self.gemini = gemini or get_gemini_client()
        self.budget = budget or self.budget
        self.job_id = job_id
        self.persist_row = persist_row  # callable(row: RowState) -> None

    def run(self, row: RowState, *, extras: dict | None = None) -> RowState:
        extras = extras or {}
        session_id = self._session_id(row)
        if row.phase == "phase1":
            row.phase1_session_id = session_id
        else:
            row.phase2_session_id = session_id
        row.phase = self.phase  # type: ignore[assignment]
        if row.started_at is None:
            row.started_at = _now()
        row.status = "running"
        self._persist(row)
        self._trace(
            session_id,
            {"type": "session_start", "row_id": row.row_id, "phase": self.phase},
        )

        t0 = time.time()
        while True:
            if row.step >= self.budget.max_steps:
                row.status = "paused_budget"
                row.status_message = "max_steps exceeded"
                break
            if (time.time() - t0) > self.budget.max_wall_seconds:
                row.status = "paused_budget"
                row.status_message = "max_wall_seconds exceeded"
                break
            if row.zi_credits_used >= self.budget.max_zi_credits:
                row.status = "paused_budget"
                row.status_message = "zi credit budget exceeded"
                break
            if row.gemini_tokens_used >= self.budget.max_gemini_tokens:
                row.status = "paused_budget"
                row.status_message = "gemini token budget exceeded"
                break

            decision = self._plan(row, session_id=session_id, extras=extras)
            if decision.get("action") == "wait":
                seconds = int(decision.get("seconds") or 5)
                self._trace(
                    session_id,
                    {"type": "wait", "seconds": seconds, "reason": decision.get("reason")},
                )
                time.sleep(min(seconds, 30))
                row.step += 1
                self._persist(row)
                continue

            if decision.get("done"):
                row.status = str(decision.get("status") or "failed")
                row.status_message = str(decision.get("reason") or "")
                if row.status in ("ready_for_review", "ready"):
                    row.completed_at = _now()
                break

            tool_name = decision.get("next_tool") or decision.get("tool")
            args = decision.get("args") or decision.get("input") or {}
            if not tool_name:
                row.status = "failed"
                row.status_message = "planner returned no next_tool"
                break

            result = self._call_tool(tool_name, args, row, session_id, extras)
            row.step += 1
            self._apply_result(row, tool_name, args, result)
            self._persist(row)

            if result.get("data", {}).get("status") == "awaiting_human":
                row.status = "awaiting_human"
                row.awaiting_human = result.get("data")
                break

        self._trace(
            session_id,
            {
                "type": "session_end",
                "status": row.status,
                "status_message": row.status_message,
            },
        )
        self._persist(row)
        return row

    def _session_id(self, row: RowState) -> str:
        existing = (
            row.phase1_session_id if self.phase == "phase1" else row.phase2_session_id
        )
        return existing or f"{self.phase}_{row.row_id}_{uuid.uuid4().hex[:8]}"

    def _persist(self, row: RowState) -> None:
        if self.persist_row:
            try:
                self.persist_row(row)
            except Exception:
                pass

    def _trace(self, session_id: str, event: dict) -> None:
        try:
            from core import drive_db

            drive_db.append_trace_event(session_id, event)
        except Exception:
            pass

    def _plan(self, row: RowState, *, session_id: str, extras: dict) -> dict:
        manifest = self.registry.manifest(phase=self.phase)
        try:
            from core import drive_db

            recent = drive_db.load_trace(session_id)[-5:]
        except Exception:
            recent = []
        remaining = {
            "steps": self.budget.max_steps - row.step,
            "zi_credits": self.budget.max_zi_credits - row.zi_credits_used,
            "gemini_tokens": self.budget.max_gemini_tokens - row.gemini_tokens_used,
        }
        prompt = (
            _load_prompt(self.prompt_file)
            + "\n\n---\n"
            + json.dumps(
                {
                    "row_state": row.model_dump(mode="json"),
                    "tool_manifest": manifest,
                    "last_trace": recent,
                    "remaining_budget": remaining,
                    "extras": {
                        k: v
                        for k, v in extras.items()
                        if k
                        in (
                            "persona_target",
                            "phase2_config",
                            "intent",
                            "instructions",
                            "source_email",
                            "style_profile",
                        )
                    },
                },
                default=str,
            )
        )
        try:
            resp = self.gemini.generate(
                self.task_kind,
                prompt,
                session_id=session_id,
                row_id=row.row_id,
                expect_json=True,
            )
            row.gemini_tokens_used += int(resp.tokens_in or 0) + int(resp.tokens_out or 0)
            decision = resp.parsed if isinstance(resp.parsed, dict) else {}
            self._trace(
                session_id,
                {
                    "type": "plan",
                    "decision": decision,
                    "tokens_in": resp.tokens_in,
                    "tokens_out": resp.tokens_out,
                    "repaired": resp.repaired,
                },
            )
            return decision
        except RateLimitedError as e:
            self._trace(session_id, {"type": "rate_limited", "error": str(e)})
            return {"action": "wait", "seconds": 15, "reason": str(e)}
        except Exception as e:
            self._trace(session_id, {"type": "plan_error", "error": str(e)})
            return {"done": True, "status": "failed", "reason": f"planner error: {e}"}

    def _call_tool(
        self,
        name: str,
        args: dict,
        row: RowState,
        session_id: str,
        extras: dict,
    ) -> dict:
        ctx = ToolContext(
            phase=self.phase,
            session_id=session_id,
            row_id=row.row_id,
            job_id=self.job_id,
            gemini=self.gemini,
            extras=extras,
        )
        t0 = time.time()
        try:
            result = self.registry.call(name, args, ctx)
            payload = result.model_dump()
        except PhaseScopeError as e:
            row.recovery_count += 1
            payload = {
                "ok": False,
                "error": str(e),
                "error_kind": "invalid_input",
                "data": None,
                "cost": {},
            }
        except Exception as e:
            payload = {
                "ok": False,
                "error": str(e),
                "error_kind": "network",
                "data": None,
                "cost": {},
            }
        wall_ms = int((time.time() - t0) * 1000)
        cost = payload.get("cost") or {}
        row.zi_credits_used += int(cost.get("zi_credits") or 0)
        row.gemini_tokens_used += int(cost.get("gemini_tokens_in") or 0) + int(
            cost.get("gemini_tokens_out") or 0
        )
        if not payload.get("ok") and payload.get("error_kind") in (
            "not_found",
            "network",
            "rate_limited",
        ):
            row.recovery_count += 1
        self._trace(
            session_id,
            {
                "type": "tool_call",
                "tool": name,
                "args": args,
                "result": {
                    "ok": payload.get("ok"),
                    "error": payload.get("error"),
                    "error_kind": payload.get("error_kind"),
                    "data_keys": list((payload.get("data") or {}).keys())
                    if isinstance(payload.get("data"), dict)
                    else None,
                    "cost": cost,
                },
                "wall_ms": wall_ms,
            },
        )
        if payload.get("error_kind") == "rate_limited":
            # surface as wait on next plan; also stash
            row.status_message = "rate_limited"
        return payload

    def _apply_result(
        self, row: RowState, tool_name: str, args: dict, result: dict
    ) -> None:
        data = result.get("data") or {}
        if not result.get("ok"):
            row.evidence.setdefault("errors", []).append(
                {"tool": tool_name, "error": result.get("error"), "kind": result.get("error_kind")}
            )
            return

        if tool_name == "resolve_domain":
            row.resolved_domain = data.get("domain") or row.resolved_domain
            row.resolved_org_name = data.get("org_name") or row.resolved_org_name
        elif tool_name == "zoominfo_search_contact":
            c = data.get("contact") or {}
            row.contact = ContactRecord(
                name=c.get("name") or "",
                email=c.get("email") or "",
                mobile=c.get("mobile") or "",
                title=c.get("title") or "",
                linkedin_url=c.get("linkedin_url") or "",
                matched_on=data.get("matched_on") or c.get("matched_on") or "",
                confidence=float(c.get("confidence") or 0.7),
            )
        elif tool_name == "manual_override_contact":
            c = data.get("contact") or {}
            row.contact = ContactRecord(**{**c, "confidence": 1.0})
        elif tool_name == "zoominfo_light_company_signal":
            row.light_org_signal = LightOrgSignal(
                industry=data.get("industry"),
                hq=data.get("hq"),
                employee_band=str(data.get("employee_band") or "") or None,
                revenue_band=str(data.get("revenue_band") or "") or None,
            )
            if data.get("org_name") and not row.resolved_org_name:
                row.resolved_org_name = data.get("org_name")
        elif tool_name == "web_find_team_page":
            row.evidence["team_page"] = data
        elif tool_name == "linkedin_person_search":
            row.evidence["linkedin"] = data
            if not row.contact and data.get("linkedin_url"):
                row.contact = ContactRecord(
                    name=data.get("name") or "",
                    title=data.get("title") or "",
                    linkedin_url=data.get("linkedin_url") or "",
                    matched_on="linkedin_person_search",
                    confidence=0.4,
                )
        elif tool_name == "zoominfo_enrich_company":
            row.evidence["company"] = data.get("company") or data
        elif tool_name == "gmail_history_lookup":
            row.evidence["gmail"] = data
        elif tool_name == "web_fetch_pages":
            row.evidence["pages"] = data.get("pages") or []
        elif tool_name == "web_find_recent_news":
            row.evidence["news"] = data.get("items") or []
        elif tool_name == "synthesize_org_brief":
            row.org_brief = data.get("org_brief") or data
            row.org_brief_ref = (row.resolved_domain or row.input or "")[:120]
        elif tool_name == "find_similar_sent_email":
            row.evidence["similar"] = data.get("emails") or []
        elif tool_name == "compose_hyper_personalized_email":
            row.draft = data.get("draft") or data
        elif tool_name == "validate_grounding":
            row.evidence["grounding"] = data
            if not data.get("ok") and row.draft:
                # annotate draft
                row.draft["grounding_violations"] = data.get("violations") or []
        elif tool_name == "inject_tracking":
            if row.draft is None:
                row.draft = {}
            row.draft["body_html"] = data.get("body_html") or row.draft.get("body_html")
            row.tracking_id = data.get("tracking_id") or row.tracking_id
        elif tool_name == "save_draft":
            row.draft_id = data.get("draft_id")
            row.tracking_id = data.get("tracking_id") or row.tracking_id
        elif tool_name == "ask_human":
            row.awaiting_human = data
