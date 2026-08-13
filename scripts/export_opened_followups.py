# NOTE: Recover tracked sent mail from Gmail, enrich Sends, export follow-up list.
from __future__ import annotations

import base64
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from config import settings  # noqa: E402

EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
OPEN_RE = re.compile(r"(?:open\?id=|/t/o/)([0-9a-fA-F-]{36})")
CLICK_RE = re.compile(r"(?:click\?id=|/t/c/)([0-9a-fA-F-]{36})")
NAME_RE = re.compile(r'^\s*"?([^"<]+?)"?\s*<')


def listing(what: str) -> list[dict]:
    url = settings.APPS_SCRIPT_TRACKING_URL
    r = requests.post(
        url, json={"action": "list", "what": what, "exclude_bots": False}, timeout=90
    )
    return r.json().get("rows") or []


def walk(part, acc: list[str]) -> None:
    if not part:
        return
    data = (part.get("body") or {}).get("data")
    if data:
        try:
            acc.append(base64.urlsafe_b64decode(data + "===").decode("utf-8", "ignore"))
        except Exception:
            pass
    for child in part.get("parts") or []:
        walk(child, acc)


def clean_eid(eid: object) -> str:
    s = str(eid or "").strip()
    if not s or "\n" in s or "http" in s:
        return ""
    if s.endswith(".gif"):
        s = s[:-4]
    return s if len(s) >= 32 else ""


def main() -> None:
    opens = listing("opens")
    clicks = listing("clicks")
    links = listing("links")

    gt = Credentials.from_authorized_user_file(str(ROOT / "credentials" / "gmail_token.json"))
    if gt.expired and gt.refresh_token:
        gt.refresh(Request())
    gmail = build("gmail", "v1", credentials=gt)

    recovered: dict[str, dict] = {}
    page = None
    fetched = 0
    while fetched < 300:
        kwargs: dict = {"userId": "me", "q": "in:sent newer_than:30d", "maxResults": 50}
        if page:
            kwargs["pageToken"] = page
        res = gmail.users().messages().list(**kwargs).execute()
        msgs = res.get("messages") or []
        if not msgs:
            break
        for m in msgs:
            fetched += 1
            full = (
                gmail.users()
                .messages()
                .get(userId="me", id=m["id"], format="full")
                .execute()
            )
            headers = {
                h["name"].lower(): h["value"]
                for h in full.get("payload", {}).get("headers", [])
            }
            parts: list[str] = []
            walk(full.get("payload"), parts)
            blob = "\n".join(parts)
            if (
                "durgaemailer-tracking" not in blob
                and "/t/o/" not in blob
                and "functions/open" not in blob
            ):
                continue
            open_ids = OPEN_RE.findall(blob)
            click_ids = CLICK_RE.findall(blob)
            to = headers.get("to", "")
            em = EMAIL_RE.search(to)
            email = em.group(0) if em else to
            nm = NAME_RE.match(to)
            info = {
                "recipient_email": email,
                "recipient_name": nm.group(1).strip() if nm else "",
                "subject": headers.get("subject", ""),
                "sent_at": headers.get("date", ""),
                "gmail_id": m["id"],
            }
            for eid in open_ids:
                recovered[eid] = info
            link_map = {l.get("link_id"): l.get("email_id") for l in links}
            for lid in click_ids:
                eid = link_map.get(lid)
                if eid:
                    recovered.setdefault(str(eid), info)
        page = res.get("nextPageToken")
        if not page:
            break

    print(f"scanned_sent={fetched} tracked_recovered={len(recovered)}")

    open_stats: dict[str, dict] = defaultdict(
        lambda: {"all": 0, "human": 0, "first": "", "last": ""}
    )
    blank_opens = 0
    for o in opens:
        eid = clean_eid(o.get("email_id"))
        if not eid:
            blank_opens += 1
            continue
        st = open_stats[eid]
        st["all"] += 1
        bot = o.get("is_bot") is True or str(o.get("is_bot")).lower() == "true"
        if not bot:
            st["human"] += 1
        ts = str(o.get("opened_at") or "")
        if ts:
            if not st["first"] or ts < st["first"]:
                st["first"] = ts
            if not st["last"] or ts > st["last"]:
                st["last"] = ts

    click_stats: dict[str, int] = defaultdict(int)
    for c in clicks:
        eid = clean_eid(c.get("email_id"))
        if eid:
            click_stats[eid] += 1

    bt = Credentials.from_authorized_user_file(
        str(ROOT / "credentials" / "bootstrap_token.json"),
        ["https://www.googleapis.com/auth/spreadsheets"],
    )
    if bt.expired and bt.refresh_token:
        bt.refresh(Request())
    sheets = build("sheets", "v4", credentials=bt)
    sid = settings.GOOGLE_SHEET_ID
    svals = (
        sheets.spreadsheets()
        .values()
        .get(spreadsheetId=sid, range="Sends")
        .execute()
        .get("values")
        or []
    )
    h = svals[0]
    cols = {n: i for i, n in enumerate(h)}
    updates = 0
    for i, row in enumerate(svals[1:], start=2):
        while len(row) < len(h):
            row.append("")
        eid = row[cols["email_id"]]
        rec = recovered.get(eid)
        if not rec:
            continue
        if row[cols["recipient_email"]] and row[cols["subject"]]:
            continue
        sheets.spreadsheets().values().update(
            spreadsheetId=sid,
            range=f"Sends!C{i}:E{i}",
            valueInputOption="RAW",
            body={
                "values": [
                    [
                        rec["recipient_email"],
                        rec.get("recipient_name", ""),
                        rec["subject"],
                    ]
                ]
            },
        ).execute()
        updates += 1
    print(f"sends_updated={updates}")

    rows = []
    for eid, info in recovered.items():
        st = open_stats.get(eid, {"all": 0, "human": 0, "first": "", "last": ""})
        rows.append(
            {
                "recipient_email": info["recipient_email"],
                "recipient_name": info.get("recipient_name", ""),
                "subject": info["subject"],
                "sent_at": info["sent_at"],
                "opens_human": st["human"],
                "opens_all": st["all"],
                "clicks": click_stats.get(eid, 0),
                "first_open": st["first"],
                "last_open": st["last"],
                "opened": st["all"] > 0,
                "email_id": eid,
            }
        )
    rows.sort(
        key=lambda r: (r["opened"], r["opens_human"], r["opens_all"], r["clicks"]),
        reverse=True,
    )
    opened = [r for r in rows if r["opened"]]
    out = {
        "tracked": rows,
        "opened": opened,
        "blank_opens": blank_opens,
        "summary": {
            "tracked_sent": len(rows),
            "opened": len(opened),
            "blank_opens": blank_opens,
        },
    }
    path = ROOT / "data" / "followups_opened.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"wrote {path}")
    print(json.dumps(out["summary"]))
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
