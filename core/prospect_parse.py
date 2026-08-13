# NOTE: Recover structured prospects from agent text when meta.prospects is missing.
from __future__ import annotations

import re
from typing import Any

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Blocks from prospect_to_text / numbered ZoomInfo dumps in Chat
_BLOCK_RE = re.compile(
    r"(?:^|\n)\s*\d+\.\s*"
    r"(?:Name:\s*(?P<name>[^\n]*)\n)?"
    r"(?:Title:\s*(?P<title>[^\n]*)\n)?"
    r"(?:Company:\s*(?P<company>[^\n]*)\n)?"
    r"(?:Email:\s*(?P<email>[^\n]*)\n)?"
    r"(?:Phone:\s*(?P<phone>[^\n]*)\n)?"
    r"(?:Mobile:\s*(?P<mobile>[^\n]*)\n)?"
    r"(?:LinkedIn:\s*(?P<linkedin>[^\n]*)\n)?",
    re.I,
)


def parse_prospects_from_agent_text(
    text: str,
    *,
    default_company: str = "",
    default_source: str = "zoominfo",
) -> list[dict[str, Any]]:
    """Pull contacts out of Chat reply text (numbered Name/Email blocks or bare emails)."""
    text = text or ""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    for m in _BLOCK_RE.finditer(text):
        email = (m.group("email") or "").strip()
        em = _EMAIL_RE.search(email)
        email = em.group(0) if em else ""
        name = (m.group("name") or "").strip()
        company = (m.group("company") or "").strip() or default_company
        title = (m.group("title") or "").strip()
        phone = (m.group("phone") or "").strip()
        mobile = (m.group("mobile") or "").strip()
        linkedin = (m.group("linkedin") or "").strip()
        if not email and not name:
            continue
        key = (email or name).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "name": name,
                "email": email,
                "company": company,
                "title": title,
                "phone": phone,
                "mobile": mobile,
                "linkedin_url": linkedin,
                "source": default_source,
                "source_id": email.lower() if email else name.lower(),
            }
        )

    if out:
        return out

    # Fallback: unique emails mentioned in the reply
    company = (default_company or "").strip()
    for em in _EMAIL_RE.findall(text):
        email = em.lower()
        if email in seen:
            continue
        # Skip obvious placeholders / system addresses
        if email in ("a@b.com", "test@test.com", "example@example.com"):
            continue
        if email.endswith("@karunamedia.org") or email.startswith("noreply"):
            continue
        seen.add(email)
        local = email.split("@")[0]
        name = " ".join(p.capitalize() for p in re.split(r"[._+\-]+", local) if p)
        out.append(
            {
                "name": name,
                "email": email,
                "company": company,
                "source": default_source,
                "source_id": email,
            }
        )
    return out
