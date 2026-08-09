# NOTE: Cooperative cancel flag for long chat runs (checked between agent steps).
from __future__ import annotations

from typing import Any, Callable, Optional

CancelCheck = Callable[[], bool]


def is_cancelled(context: Optional[dict[str, Any]] = None) -> bool:
    """Return True when the UI asked to stop the current operation."""
    if not context:
        return False
    check = context.get("cancel_check")
    if callable(check):
        try:
            return bool(check())
        except Exception:
            return False
    return bool(context.get("cancelled") or context.get("stop"))


def stopped_message() -> str:
    return "\n\n_(⏹ Stopped by you — edit your last message and resubmit if needed.)_\n"
