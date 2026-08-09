# NOTE: Shared volume caps — research/search/draft scale with the user request up to 100.
from __future__ import annotations

import re
from typing import Any

MAX_EMAILS = 100
MAX_ORGS = 100
MAX_CONTACTS = 100
MAX_CONTACTS_PER_ORG = 25

DEFAULT_ORGS = 25
DEFAULT_CONTACTS_PER_ORG = 5
DEFAULT_SEARCH_LIMIT = 50


def clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(n)))


def parse_research_limits(user_msg: str) -> dict[str, int]:
    """Infer org / contact / email volumes from the user wording.

    Examples:
      - "find 40 NGOs" → org_limit=40
      - "100 emails" / "draft 100" → email_limit=100, search≈100
      - "as many contacts as needed" → max caps
    """
    msg = user_msg or ""
    org_limit = DEFAULT_ORGS
    contacts_per_org = DEFAULT_CONTACTS_PER_ORG
    search_limit = DEFAULT_SEARCH_LIMIT
    email_limit = MAX_EMAILS

    # Explicit counts
    m = re.search(
        r"\b(\d{1,3})\s*(?:ngos?|orgs?|organizations?|foundations?|trusts?)\b",
        msg,
        re.I,
    )
    if m:
        org_limit = clamp(int(m.group(1)), 1, MAX_ORGS)

    m = re.search(
        r"\b(?:find|search|list|get|pull|need|want)\s+(\d{1,3})\b"
        r"(?!\s*(?:emails?|drafts?|contacts?|people|prospects?|leads?))",
        msg,
        re.I,
    )
    if m and not re.search(
        r"\b(?:ngos?|orgs?|organizations?|emails?|contacts?)\b",
        m.group(0),
        re.I,
    ):
        # "find 50 …" without a clear noun — treat as org/search volume when researching
        n = clamp(int(m.group(1)), 1, MAX_ORGS)
        # Only apply if the number is near an org-related word later in the sentence
        tail = msg[m.end() : m.end() + 40]
        if re.search(
            r"\b(ngos?|orgs?|organizations?|foundations?|trusts?)\b", tail, re.I
        ):
            org_limit = max(org_limit, n)
            search_limit = max(search_limit, n)

    m = re.search(
        r"\b(\d{1,3})\s*(?:emails?|drafts?|messages?|outreach)\b",
        msg,
        re.I,
    )
    if m:
        email_limit = clamp(int(m.group(1)), 1, MAX_EMAILS)
        search_limit = max(search_limit, email_limit)
        # Enough orgs to hopefully yield that many emails
        org_limit = max(org_limit, clamp((email_limit + 2) // 2, 1, MAX_ORGS))

    m = re.search(
        r"\b(\d{1,3})\s*(?:contacts?|people|prospects?|leads?)\b",
        msg,
        re.I,
    )
    if m:
        search_limit = clamp(int(m.group(1)), 1, MAX_CONTACTS)
        email_limit = max(email_limit, search_limit)
        org_limit = max(org_limit, clamp((search_limit + 2) // 3, 1, MAX_ORGS))

    m = re.search(
        r"\b(\d{1,2})\s*(?:contacts?|people|emails?)\s*(?:per|\/)\s*org\b",
        msg,
        re.I,
    )
    if m:
        contacts_per_org = clamp(int(m.group(1)), 1, MAX_CONTACTS_PER_ORG)

    # Open-ended wording → push toward max useful research
    if re.search(
        r"\b("
        r"as many as (?:needed|possible|you can)|"
        r"all (?:available|matching)|"
        r"no limit|"
        r"don'?t (?:limit|restrict)|"
        r"broad (?:search|research)|"
        r"comprehensive|"
        r"large (?:list|batch)"
        r")\b",
        msg,
        re.I,
    ):
        org_limit = max(org_limit, 50)
        search_limit = max(search_limit, 100)
        email_limit = MAX_EMAILS
        contacts_per_org = max(contacts_per_org, 5)

    return {
        "org_limit": clamp(org_limit, 1, MAX_ORGS),
        "contacts_per_org": clamp(contacts_per_org, 1, MAX_CONTACTS_PER_ORG),
        "search_limit": clamp(search_limit, 1, MAX_CONTACTS),
        "email_limit": clamp(email_limit, 1, MAX_EMAILS),
    }


def apply_email_cap(
    jobs: list[dict[str, Any]],
    *,
    email_limit: int = MAX_EMAILS,
) -> list[dict[str, Any]]:
    """Keep at most email_limit draft/send jobs (default 100)."""
    cap = clamp(email_limit, 1, MAX_EMAILS)
    return list(jobs)[:cap]
