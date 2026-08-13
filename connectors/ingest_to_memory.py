# NOTE: Skips error rows when ingesting so a single provider failure won't pollute memory.
from __future__ import annotations

import sys
from typing import Any

import pandas as pd

from core import memory


def prospect_to_text(p: dict[str, Any]) -> str:
    """Flatten a normalized prospect into an embed-friendly multi-line string."""
    lines = [
        f"Name: {p.get('name') or ''}",
        f"Title: {p.get('title') or ''}",
        f"Company: {p.get('company') or ''}",
        f"Email: {p.get('email') or ''}",
        f"Phone: {p.get('phone') or ''}",
        f"Mobile: {p.get('mobile') or ''}",
        f"LinkedIn: {p.get('linkedin_url') or ''}",
        f"Location: {p.get('location') or ''}",
        f"Seniority: {p.get('seniority') or ''}",
        f"Department: {p.get('department') or ''}",
        f"Source: {p.get('source') or ''}",
    ]
    return "\n".join(lines)


def ingest_prospects(
    prospects: list[dict[str, Any]],
    source_tag: str = "prospects",
    *,
    persist_list: bool = True,
) -> list[str]:
    """Add prospects to the durable Drive list first, then Chroma/JSONL memory.

    Drive persistence must not depend on memory/Chroma succeeding — otherwise
    ZoomInfo hits appear in chat but never land in relay_prospects.json.

    Set persist_list=False for mailbox-derived contacts so Gmail noise does not
    overwrite the ZoomInfo prospect list (and thrash Drive during boot sync).
    """
    clean: list[dict[str, Any]] = []
    for p in prospects:
        if not p or p.get("error"):
            continue
        clean.append(p)

    if clean and persist_list:
        try:
            from core.prospect_list import save_prospects

            n = save_prospects(clean)
            print(
                f"[ingest] prospect_list saved/updated {n} "
                f"(from {len(clean)} rows, source={source_tag})",
                file=sys.stderr,
            )
        except Exception as e:
            print(
                f"[ingest] prospect_list save FAILED ({source_tag}): {e}",
                file=sys.stderr,
            )
    elif clean and not persist_list:
        print(
            f"[ingest] memory-only {len(clean)} rows (source={source_tag}, "
            f"skip prospect_list)",
            file=sys.stderr,
        )

    ids: list[str] = []
    batch: list[dict[str, Any]] = []
    for p in clean:
        text = prospect_to_text(p)
        title = f"{p.get('name') or 'Unknown'} @ {p.get('company') or '?'}"
        batch.append(
            {
                "text": text,
                "source": source_tag,
                "source_id": str(p.get("source_id") or p.get("email") or ""),
                "title": title,
                "metadata": {
                    "email": p.get("email") or "",
                    "company": p.get("company") or "",
                    "provider": p.get("source") or "",
                    "name": p.get("name") or "",
                },
            }
        )
    if batch:
        try:
            ids = memory.add_batch(batch)
        except Exception as e:
            print(f"[ingest] memory batch add skipped ({source_tag}): {e}", file=sys.stderr)
            for item in batch:
                try:
                    ids.extend(
                        memory.add(
                            texts=item["text"],
                            source=item["source"],
                            source_id=item.get("source_id"),
                            title=item.get("title"),
                            metadata=item.get("metadata"),
                        )
                    )
                except Exception as e2:
                    print(
                        f"[ingest] memory add skipped for "
                        f"{(item.get('metadata') or {}).get('email') or '?'}: {e2}",
                        file=sys.stderr,
                    )
    return ids


def prospects_to_dataframe(prospects: list[dict[str, Any]]) -> pd.DataFrame:
    """Return a tidy DataFrame of prospect fields (name never shows a raw email)."""
    from connectors import sanitize_prospect

    cols = [
        "name",
        "title",
        "company",
        "email",
        "phone",
        "mobile",
        "linkedin_url",
        "location",
        "seniority",
        "department",
        "source",
    ]
    rows = []
    for p in prospects:
        if p.get("error"):
            continue
        clean = sanitize_prospect(p)
        rows.append({c: clean.get(c, "") for c in cols})
    return pd.DataFrame(rows, columns=cols)
