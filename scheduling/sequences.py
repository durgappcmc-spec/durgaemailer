# NOTE: Default timezone offset -5 (US Eastern). Adjust via tz_offset_hours if needed.
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from scheduling.client import schedule_batch


def _shift_to_business_hours(
    dt: datetime,
    tz_offset_hours: int = -5,
) -> datetime:
    """Push weekends to Monday 9:30am local; clamp hours to 9–17."""
    # Treat naive datetimes as already-local; apply offset only for awareness notes.
    local = dt
    if local.tzinfo is not None:
        # Convert toward intended offset roughly by replacing tzinfo-naive local wall time
        local = local.replace(tzinfo=None) + timedelta(hours=tz_offset_hours) - timedelta(
            hours=int(dt.utcoffset().total_seconds() // 3600) if dt.utcoffset() else 0
        )

    # Weekend → next Monday 9:30
    if local.weekday() >= 5:  # Sat=5 Sun=6
        days = 7 - local.weekday()
        local = local.replace(hour=9, minute=30, second=0, microsecond=0) + timedelta(
            days=days
        )

    if local.hour < 9:
        local = local.replace(hour=9, minute=30, second=0, microsecond=0)
    elif local.hour >= 17:
        local = local + timedelta(days=1)
        local = local.replace(hour=9, minute=30, second=0, microsecond=0)
        if local.weekday() >= 5:
            days = 7 - local.weekday()
            local = local + timedelta(days=days)

    return local


def schedule_sequence(
    prospect: dict[str, Any],
    steps: list[dict[str, Any]],
    start_at: Optional[datetime] = None,
    campaign: Optional[str] = None,
    business_hours_only: bool = True,
) -> dict[str, Any]:
    """Schedule a drip sequence for one prospect.

    Each step: {delay_days, delay_hours, subject, html_body, attachments}.
    """
    recipient_email = prospect.get("email") or prospect.get("recipient_email")
    if not recipient_email:
        return {"ok": False, "error": "prospect has no email"}

    start = start_at or datetime.now()
    jobs: list[dict[str, Any]] = []
    cursor = start
    for step in steps:
        delay = timedelta(
            days=int(step.get("delay_days") or 0),
            hours=int(step.get("delay_hours") or 0),
        )
        cursor = cursor + delay
        send_at = (
            _shift_to_business_hours(cursor) if business_hours_only else cursor
        )
        jobs.append(
            {
                "recipient_email": recipient_email,
                "recipient_name": prospect.get("name")
                or prospect.get("recipient_name")
                or "",
                "subject": step.get("subject") or "",
                "html_body": step.get("html_body") or "",
                "send_at": send_at.isoformat(),
                "campaign": campaign or prospect.get("campaign") or "",
                "source": prospect.get("source") or "sequence",
                "attachments": step.get("attachments") or [],
            }
        )
    return schedule_batch(jobs)
