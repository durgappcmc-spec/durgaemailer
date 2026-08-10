# NOTE: Direct Sheets writes when Apps Script web app is behind; never blocks sends.
from __future__ import annotations

import sys
from datetime import datetime, timezone

from core.google_sheets import sheet_id, sheets_service


def seed_send_row(
    *,
    email_id: str,
    recipient_email: str = "",
    recipient_name: str = "",
    subject: str = "",
    campaign: str = "",
    source: str = "relay_draft",
) -> bool:
    """Append a Sends row if email_id is not already present. Returns True on write."""
    if not email_id:
        return False
    sid = sheet_id()
    svc = sheets_service()
    if not sid or not svc:
        return False
    try:
        existing = (
            svc.spreadsheets()
            .values()
            .get(spreadsheetId=sid, range="Sends!A:B")
            .execute()
            .get("values")
            or []
        )
        for row in existing[1:]:
            if len(row) > 1 and str(row[1]) == str(email_id):
                return False
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        svc.spreadsheets().values().append(
            spreadsheetId=sid,
            range="Sends",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={
                "values": [
                    [
                        now,
                        email_id,
                        recipient_email or "",
                        recipient_name or "",
                        subject or "",
                        campaign or "",
                        source or "relay_draft",
                        "",
                        "",
                    ]
                ]
            },
        ).execute()
        return True
    except Exception as e:
        print(f"[tracking] seed_send_row failed: {e}", file=sys.stderr)
        return False
