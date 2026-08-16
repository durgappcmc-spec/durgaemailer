# NOTE: Tracking sent/open times are stored as UTC; display them in IST (no DST).
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

IST = timezone(timedelta(hours=5, minutes=30))


def parse_tracking_dt(val: Any) -> datetime | None:
    """Parse a Sheets/Apps Script timestamp. Naive values are treated as UTC."""
    if val is None or val == "":
        return None
    if isinstance(val, datetime):
        dt = val
    else:
        text = str(val).strip()
        if not text:
            return None
        try:
            from dateutil import parser as date_parser

            dt = date_parser.parse(text)
        except (ValueError, OverflowError, TypeError):
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def format_ist(val: Any) -> str:
    """Human IST stamp, e.g. '16 Aug 2026, 4:05 PM IST'. Empty input stays empty."""
    dt = parse_tracking_dt(val)
    if dt is None:
        return str(val or "").strip()
    local = dt.astimezone(IST)
    return local.strftime("%d %b %Y, %H:%M IST")
