# NOTE: Tracking first/last open display is IST (UTC+5:30).
from core.ist_time import format_ist, parse_tracking_dt


def test_format_ist_from_utc_iso():
    assert format_ist("2026-08-16T10:35:00.000Z") == "16 Aug 2026, 16:05 IST"


def test_format_ist_from_offset():
    assert format_ist("2026-08-16T16:05:00+05:30") == "16 Aug 2026, 16:05 IST"


def test_format_ist_naive_is_utc():
    assert format_ist("2026-08-16T10:35:00") == "16 Aug 2026, 16:05 IST"


def test_format_ist_empty():
    assert format_ist("") == ""
    assert format_ist(None) == ""


def test_parse_tracking_dt_aware():
    dt = parse_tracking_dt("2026-08-16T10:35:00Z")
    assert dt is not None
    assert dt.utcoffset() is not None
    assert format_ist(dt) == "16 Aug 2026, 16:05 IST"
