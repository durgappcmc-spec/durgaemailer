# NOTE: One-shot migration of existing Relay Drive/local durable data into DurgaEmailer/ layout.
"""Migrate chats, prospects, aliases from durable_store / Drive into drive_db indexes.

Usage:
  python scripts/migrate_db_to_drive.py [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate Relay durable data to drive_db")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    from core import drive_db, drive_store, durable_store

    print("[migrate] ensuring indexes…")
    if not args.dry_run:
        drive_db.ensure_indexes()

    # Chat messages
    chat_payload = None
    try:
        chat_payload = durable_store.load_chat_messages(allow_sheets=False)
    except Exception:
        chat_payload = None
    if not chat_payload:
        chat_payload = drive_store.download_json("relay_chat.json")

    migrated_chats = 0
    if isinstance(chat_payload, list) and chat_payload:
        chat_id = f"migrated_{uuid.uuid4().hex[:8]}"
        chat = {
            "chat_id": chat_id,
            "title": "Migrated chat",
            "created_at": _now(),
            "messages": chat_payload,
            "source": "relay_chat.json",
        }
        print(f"[migrate] chat messages: {len(chat_payload)} → {chat_id}")
        if not args.dry_run:
            drive_db.save_chat(chat_id, chat)
        migrated_chats = 1
    else:
        print("[migrate] no chat messages found")

    # Prospects → org domain cache seeds
    prospects = None
    try:
        prospects = durable_store.load_json_blob("prospect_list", allow_sheets=False)
    except Exception:
        prospects = None
    if prospects is None:
        prospects = drive_store.download_json("relay_prospects.json")

    cache_hits = 0
    if isinstance(prospects, list):
        cache = drive_db.load_org_domain_cache() if not args.dry_run else {}
        for row in prospects:
            if not isinstance(row, dict):
                continue
            name = (
                row.get("company")
                or row.get("organization")
                or row.get("org")
                or row.get("name")
                or ""
            )
            domain = row.get("domain") or row.get("website") or row.get("company_domain")
            if name and domain:
                key = str(name).strip().lower()
                cache[key] = {
                    "domain": str(domain).strip().lower().replace("https://", "").replace("http://", "").split("/")[0],
                    "org_name": str(name),
                    "updated_at": _now(),
                    "source": "migrated_prospects",
                }
                cache_hits += 1
        print(f"[migrate] org_domain_cache seeds: {cache_hits}")
        if not args.dry_run and cache_hits:
            drive_db.save_org_domain_cache(cache)
    else:
        print("[migrate] no prospects found")

    # Aliases (best-effort note in persona targets empty list)
    aliases = None
    try:
        aliases = durable_store.load_json_blob("contact_aliases", allow_sheets=False)
    except Exception:
        aliases = None
    if aliases is None:
        aliases = drive_store.download_json("relay_aliases.json")
    if aliases:
        print(f"[migrate] contact_aliases present ({type(aliases).__name__}) — left in place; not rewritten")
    else:
        print("[migrate] no contact_aliases")

    # Default persona presets
    if not args.dry_run:
        existing = drive_db.load_persona_targets()
        if not existing:
            drive_db.save_persona_targets(
                [
                    {
                        "id": "csr_head",
                        "label": "CSR Head",
                        "titles": [
                            "CSR Head",
                            "Head of CSR",
                            "CSR Manager",
                            "Head of Partnerships",
                            "Director of Partnerships",
                            "Head of Corporate Partnerships",
                        ],
                        "seniority": ["Director", "VP", "C-Level", "Manager"],
                    },
                    {
                        "id": "fundraising_lead",
                        "label": "Fundraising Lead",
                        "titles": [
                            "Head of Fundraising",
                            "Director of Development",
                            "Chief Development Officer",
                            "Fundraising Manager",
                        ],
                        "seniority": ["Director", "VP", "C-Level", "Manager"],
                    },
                ]
            )
            print("[migrate] seeded default persona_targets")

    print(
        f"[migrate] done dry_run={args.dry_run} chats={migrated_chats} domain_seeds={cache_hits}"
    )
    print(
        "[migrate] Sheets tabs (Sends/Opens/Clicks/Scheduled) left untouched — live tracking store"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
