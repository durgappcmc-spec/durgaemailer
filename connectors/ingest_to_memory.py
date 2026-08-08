# NOTE: Skips error rows when ingesting so a single provider failure won't pollute memory.
from __future__ import annotations

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
) -> list[str]:
    """Add prospects to Chroma memory. Skips rows with errors."""
    ids: list[str] = []
    for p in prospects:
        if not p or p.get("error"):
            continue
        text = prospect_to_text(p)
        title = f"{p.get('name') or 'Unknown'} @ {p.get('company') or '?'}"
        added = memory.add(
            texts=text,
            source=source_tag,
            source_id=str(p.get("source_id") or p.get("email") or ""),
            title=title,
            metadata={
                "email": p.get("email") or "",
                "company": p.get("company") or "",
                "provider": p.get("source") or "",
            },
        )
        ids.extend(added)
    return ids


def prospects_to_dataframe(prospects: list[dict[str, Any]]) -> pd.DataFrame:
    """Return a tidy DataFrame of prospect fields."""
    cols = [
        "name",
        "title",
        "company",
        "email",
        "phone",
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
        rows.append({c: p.get(c, "") for c in cols})
    return pd.DataFrame(rows, columns=cols)
