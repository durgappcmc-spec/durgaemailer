# NOTE: Phase-scoped tool registry.
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError

from core.tools.base import Tool, ToolContext, ToolResult


class PhaseScopeError(PermissionError):
    pass


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise KeyError(f"unknown tool: {name}")
        return self._tools[name]

    def manifest(self, phase: str | None = None) -> list[dict]:
        out: list[dict] = []
        for t in self._tools.values():
            scope = set(getattr(t, "phase_scope", set()) or set())
            if phase is not None and phase not in scope:
                continue
            out.append(
                {
                    "name": t.name,
                    "description": t.description,
                    "phase_scope": sorted(scope),
                    "idempotent": bool(getattr(t, "idempotent", False)),
                    "cost_hint": getattr(t, "cost_hint", {}) or {},
                    "input_schema": _schema_dict(t.input_schema),
                }
            )
        return out

    def call(self, name: str, inputs: dict, ctx: ToolContext) -> ToolResult:
        tool = self.get(name)
        scope = set(getattr(tool, "phase_scope", set()) or set())
        if ctx.phase not in scope:
            raise PhaseScopeError(
                f"tool {name} not in scope for phase {ctx.phase}; allowed={sorted(scope)}"
            )
        try:
            model = tool.input_schema.model_validate(inputs or {})
        except ValidationError as e:
            return ToolResult(
                ok=False,
                error=str(e),
                error_kind="invalid_input",
            )
        return tool.run(model, ctx)


def _schema_dict(model: type[BaseModel]) -> dict[str, Any]:
    try:
        return model.model_json_schema()
    except Exception:
        return {"title": getattr(model, "__name__", "Input")}


_REGISTRY: ToolRegistry | None = None


def get_registry() -> ToolRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        from core.tools import build_registry

        _REGISTRY = build_registry()
    return _REGISTRY


def reset_registry() -> None:
    global _REGISTRY
    _REGISTRY = None
