# NOTE: Creates a Google Sheet with Relay tabs via Sheets API using Gmail OAuth
# desktop credentials (same JSON as Gmail). Does not push Apps Script by itself —
# use clasp or the manual paste steps printed at the end.
from __future__ import annotations

import json
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

ROOT = Path(__file__).resolve().parents[1]
SECRETS = ROOT / "credentials" / "gmail_oauth.json"
TOKEN = ROOT / "credentials" / "bootstrap_token.json"
ENV_PATH = ROOT / ".env"

# Sheets + Drive + script-ish scopes for bootstrap
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

HEADERS = {
    "Scheduled": [
        "queued_at",
        "send_at",
        "status",
        "recipient_email",
        "recipient_name",
        "subject",
        "html_body",
        "campaign",
        "source",
        "attachments_json",
        "email_id",
        "error",
        "attempts",
        "thread_id",
        "gmail_message_id",
    ],
    "Sends": [
        "sent_at",
        "email_id",
        "recipient_email",
        "recipient_name",
        "subject",
        "campaign",
        "source",
        "thread_id",
        "gmail_message_id",
    ],
    "Opens": [
        "opened_at",
        "email_id",
        "ip",
        "user_agent",
        "is_bot",
        "is_first_open",
    ],
    "Clicks": [
        "clicked_at",
        "link_id",
        "email_id",
        "ip",
        "user_agent",
        "is_bot",
    ],
    "Links": ["link_id", "email_id", "original_url", "label"],
    "Replies": [
        "detected_at",
        "recipient_email",
        "thread_id",
        "original_email_id",
        "original_campaign",
        "reply_snippet",
        "cancelled_count",
    ],
}


def get_creds() -> Credentials:
    creds: Credentials | None = None
    if TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN.write_text(creds.to_json(), encoding="utf-8")
        return creds
    if not SECRETS.exists():
        raise FileNotFoundError(
            f"Missing {SECRETS}. Download a Desktop OAuth client JSON first.\n"
            "Also enable Google Sheets API + Drive API on the same Cloud project."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(SECRETS), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN.parent.mkdir(parents=True, exist_ok=True)
    TOKEN.write_text(creds.to_json(), encoding="utf-8")
    return creds


def upsert_env(key: str, value: str) -> None:
    lines: list[str] = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    else:
        example = ROOT / ".env.example"
        lines = example.read_text(encoding="utf-8").splitlines() if example.exists() else []
    found = False
    out: list[str] = []
    for line in lines:
        if line.startswith(f"{key}="):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")


def main() -> int:
    print("[bootstrap] Authenticating…")
    creds = get_creds()
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)

    title = "Relay Tracking DB"
    print(f"[bootstrap] Creating spreadsheet '{title}'…")
    created = (
        sheets.spreadsheets()
        .create(
            body={
                "properties": {"title": title},
                "sheets": [{"properties": {"title": name}} for name in HEADERS],
            }
        )
        .execute()
    )
    sheet_id = created["spreadsheetId"]
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
    print(f"[bootstrap] Sheet ID: {sheet_id}")
    print(f"[bootstrap] URL: {url}")

    # Write headers
    data = []
    for name, headers in HEADERS.items():
        data.append({"range": f"'{name}'!A1", "values": [headers]})
    sheets.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "RAW", "data": data},
    ).execute()

    # Patch Apps Script placeholder
    gs_path = ROOT / "apps_script" / "Code.gs"
    gs = gs_path.read_text(encoding="utf-8")
    gs = gs.replace("PASTE_YOUR_GOOGLE_SHEET_ID_HERE", sheet_id)
    gs_path.write_text(gs, encoding="utf-8")
    print("[bootstrap] Updated apps_script/Code.gs SHEET_ID")

    upsert_env("GOOGLE_SHEET_ID", sheet_id)
    print("[bootstrap] Wrote GOOGLE_SHEET_ID to .env")

    # Write clasp-friendly appsscript.json
    appsscript = {
        "timeZone": "America/New_York",
        "dependencies": {},
        "exceptionLogging": "STACKDRIVER",
        "runtimeVersion": "V8",
        "webapp": {
            "executeAs": "USER_DEPLOYING",
            "access": "ANYONE_ANONYMOUS",
        },
    }
    (ROOT / "apps_script" / "appsscript.json").write_text(
        json.dumps(appsscript, indent=2), encoding="utf-8"
    )

    print(
        """
[bootstrap] Next — push Apps Script:

  npm i -g @google/clasp
  clasp login
  cd apps_script
  clasp create --type standalone --title "Relay Scheduler" --rootDir .
  clasp push
  # In the Apps Script editor: run setup(), installTrigger(), installReplyWatcher()
  # Deploy → Web App → Execute as Me, Anyone → copy URL to APPS_SCRIPT_TRACKING_URL

Or paste apps_script/Code.gs manually at script.google.com (SHEET_ID already filled).
"""
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"[bootstrap] failed: {e}", file=sys.stderr)
        raise SystemExit(1)
