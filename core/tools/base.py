# NOTE: Tool protocol + ToolResult + ToolContext.
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class ToolResult(BaseModel):
    ok: bool
    data: dict[str, Any] | None = None
    error: str | None = None
    error_kind: str | None = None
    cost: dict[str, Any] = Field(default_factory=dict)
    citations: list[dict] = Field(default_factory=list)


@dataclass
class ToolContext:
    phase: str  # "phase1" | "phase2"
    session_id: str
    row_id: str
    job_id: str | None = None
    gemini: Any = None
    extras: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Tool(Protocol):
    name: str
    description: str
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    cost_hint: dict
    idempotent: bool
    phase_scope: set[str]

    def run(self, inputs: BaseModel, ctx: ToolContext) -> ToolResult: ...
