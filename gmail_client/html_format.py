# NOTE: Convert markdown / plain text into email-safe HTML; strip residual ** markers.
from __future__ import annotations

import re
from typing import Optional

_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_BOLD_US = re.compile(r"__(.+?)__")
_MD_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])")
_MD_ITALIC_US = re.compile(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])")
_URL_RE = re.compile(r"(https?://[^\s<]+)")
_TRACKING_URL_RE = re.compile(
    r"(?:/\.netlify/functions/(?:click|open)|/t/[co]/|durgaemailer-tracking\.netlify\.app)",
    re.I,
)
_HTML_GREETING_RE = re.compile(
    r"(<(?:p|div)[^>]*>\s*)(Dear|Hi|Hello)(\s+)([^,<\n]{1,80}?)(?=\s*[,:<])",
    re.I,
)
_PLAIN_GREETING_RE = re.compile(
    r"(^|\n)(Dear|Hi|Hello)(\s+)([^,\n<]{1,80}?)(?=\s*[,:\n]|$)",
    re.I,
)


def _is_tracking_url(url: str) -> bool:
    return bool(url) and bool(_TRACKING_URL_RE.search(url))


def rewrite_opening_greeting(html: str, *, first_name: str = "") -> str:
    """Point the first Dear/Hi/Hello line at this recipient, not a cloned name."""
    body = html or ""
    first = (first_name or "").strip()
    if not body or not first:
        return body

    def _already_this_person(old: str) -> bool:
        core = re.sub(r"\s*\([^)]*\)\s*", " ", old or "").strip()
        if not core:
            return False
        token = core.split()[0].strip(".,;:")
        return token.lower() == first.lower() or core.lower().startswith(first.lower())

    def _repl(m: re.Match) -> str:
        old = m.group(4)
        if _already_this_person(old):
            return m.group(0)
        return f"{m.group(1)}{m.group(2)}{m.group(3)}{first}"

    updated, n = _HTML_GREETING_RE.subn(_repl, body, count=1)
    if n:
        return updated
    updated, n = _PLAIN_GREETING_RE.subn(_repl, body, count=1)
    return updated if n else body


def ensure_designation_in_greeting(
    html: str,
    *,
    first_name: str = "",
    title: str = "",
) -> str:
    """Rewrite the opening greeting to this recipient, then add (designation)."""
    body = rewrite_opening_greeting(html or "", first_name=first_name)
    title = (title or "").strip()
    first = (first_name or "").strip()
    if not body or not title:
        return body
    greet = body[:800]
    if first and f"{first} ({title})" in greet:
        return body
    if first:
        pat = (
            rf"(<(?:p|div)[^>]*>\s*(?:Dear|Hi|Hello)\s+)"
            rf"({re.escape(first)})(\b)"
        )
        updated, n = re.subn(pat, rf"\1\2 ({title})\3", body, count=1, flags=re.I)
        if n:
            return updated
    pat2 = r"(<(?:p|div)[^>]*>\s*(?:Dear|Hi|Hello)\s+)([^,<]{1,60}?)(\s*,)"
    updated, n = re.subn(
        pat2, rf"\1\2 ({title})\3", body, count=1, flags=re.I
    )
    return updated if n else body


def apply_inline_markdown(text: str, *, escape_html: bool = True) -> str:
    """Turn **bold** / *italic* into HTML; optionally escape first."""
    s = text or ""
    try:
        from core.tracking import strip_visible_tracking_urls

        s = strip_visible_tracking_urls(s)
    except Exception:
        pass
    if escape_html:
        s = (
            s.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
    s = _MD_BOLD.sub(r"<strong>\1</strong>", s)
    s = _MD_BOLD_US.sub(r"<strong>\1</strong>", s)
    s = _MD_ITALIC.sub(r"<em>\1</em>", s)
    s = _MD_ITALIC_US.sub(r"<em>\1</em>", s)

    def _linkify(m: re.Match) -> str:
        url = m.group(1)
        if _is_tracking_url(url):
            return ""
        return f'<a href="{url}">{url}</a>'

    s = _URL_RE.sub(_linkify, s)
    return s


def plain_or_markdown_to_html(text: str) -> str:
    """Convert a full plain/markdown email body into simple HTML paragraphs/lists."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    # Already HTML-ish
    if re.search(r"</?(?:p|div|br|ul|ol|li|strong|em|a)\b", text, re.I):
        return render_markdown_in_html(text)

    blocks = re.split(r"\n\s*\n+", text)
    parts: list[str] = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        lines = [ln.rstrip() for ln in block.split("\n")]
        lines = [ln for ln in lines if ln.strip()]
        if not lines:
            continue
        # Bullet / numbered list
        if all(re.match(r"^(\-|\*|\u2022|\d+[\.\)])\s+", ln.strip()) for ln in lines):
            items = []
            for ln in lines:
                item = re.sub(r"^(\-|\*|\u2022|\d+[\.\)])\s+", "", ln.strip())
                items.append(f"<li>{apply_inline_markdown(item)}</li>")
            parts.append("<ul>\n" + "\n".join(items) + "\n</ul>")
            continue
        if len(lines) > 1:
            inner = "<br>\n".join(apply_inline_markdown(ln.strip()) for ln in lines)
            parts.append(f"<p>{inner}</p>")
        else:
            line = lines[0].strip()
            if (
                len(line) < 120
                and not line.endswith(".")
                and not line.lower().startswith("http")
                and (
                    line.endswith(":")
                    or "—" in line
                    or re.match(
                        r"^(The opportunity|Why |What your|See our|Success stories|"
                        r"AI-integrated|Craft &|Full channel|Next step|Thanks,?)",
                        line,
                        re.I,
                    )
                )
            ):
                parts.append(f"<p><strong>{apply_inline_markdown(line)}</strong></p>")
            else:
                parts.append(f"<p>{apply_inline_markdown(line)}</p>")
    return "\n".join(parts)


def render_markdown_in_html(html: str) -> str:
    """Replace leftover **bold** / *italic* inside HTML text nodes."""
    if not html:
        return html or ""
    if "**" not in html and not re.search(r"(?<!\w)\*(?!\*)", html):
        # Still may have single * italics; cheap path when no markers
        if "*" not in html and "_" not in html:
            return html
    try:
        from bs4 import BeautifulSoup
        from bs4 import NavigableString

        soup = BeautifulSoup(html, "html.parser")
        for node in list(soup.find_all(string=True)):
            if not isinstance(node, NavigableString):
                continue
            parent = node.parent
            if parent is None or parent.name in ("script", "style", "code", "pre"):
                continue
            raw = str(node)
            if "*" not in raw and "_" not in raw:
                continue
            converted = apply_inline_markdown(raw, escape_html=False)
            if converted == raw:
                continue
            # Parsed fragment may include <strong>/<em>/<a>
            frag = BeautifulSoup(converted, "html.parser")
            node.replace_with(frag)
        return str(soup)
    except Exception:
        # Fallback: crude global replace (safe enough after escape)
        return apply_inline_markdown(html, escape_html=False)


def normalize_email_html(body: str) -> str:
    """Ensure draft/send body is real HTML with markdown rendered (no raw **)."""
    body = (body or "").strip()
    if not body:
        return "<p></p>"
    if re.search(r"</?(?:p|div|br|ul|ol|li|table|strong|em|a|h\d)\b", body, re.I):
        return render_markdown_in_html(body)
    return plain_or_markdown_to_html(body) or "<p></p>"


def body_looks_signed(html: str, signature_html: str = "") -> bool:
    """True when the body already contains a real signature (not just 'Regards,')."""
    if not html:
        return False
    compact = re.sub(r"\s+", " ", html).lower()
    tail = compact[-1500:]
    # Strong org / contact signals only — a closing salutation alone is not a signature
    if (
        "karunamedia" in tail
        or "karuna media" in tail
        or "csr@karunamedia" in tail
        or "linkedin.com/in" in tail
        or re.search(r"\+\d[\d\s\-()]{7,}", tail)
    ):
        return True
    if signature_html:
        try:
            from bs4 import BeautifulSoup

            sig_text = BeautifulSoup(signature_html, "html.parser").get_text(
                " ", strip=True
            )
        except Exception:
            sig_text = re.sub(r"<[^>]+>", " ", signature_html)
        sig_text = re.sub(r"\s+", " ", sig_text).strip().lower()
        if len(sig_text) >= 12:
            body_compact = re.sub(r"\s+", "", compact)
            for n in (80, 40, 24):
                needle = re.sub(r"\s+", "", sig_text[:n])
                if len(needle) >= 12 and needle in body_compact:
                    return True
    return False


_SALUTATION_RE = re.compile(
    r"^(thanks,?|thank you|best regards|warm regards|kind regards|regards)\s*,?\s*$",
    re.I,
)


def strip_trailing_signature_block(html: str) -> str:
    """Remove a trailing signature after a closing salutation (Best/Warm regards)."""
    if not (html or "").strip():
        return html or ""

    def _after_looks_like_sig(after: str) -> bool:
        after = (after or "").strip()
        if len(after) < 20:
            return False
        after_l = after.lower()
        return bool(
            "karunamedia" in after_l
            or "karuna media" in after_l
            or re.search(r"@\w+\.\w+", after)
            or re.search(r"\+\d", after)
            or "linkedin" in after_l
        )

    try:
        from bs4 import BeautifulSoup
        from bs4 import NavigableString

        soup = BeautifulSoup(html, "html.parser")
        candidates = []
        for el in soup.find_all(["p", "div", "span", "td"]):
            t = el.get_text(" ", strip=True)
            if _SALUTATION_RE.match(t):
                candidates.append(el)
        if candidates:
            cut = candidates[-1]
            # Plain text after salutation (siblings / following content)
            after_bits: list[str] = []
            for sib in cut.next_siblings:
                if getattr(sib, "get_text", None):
                    after_bits.append(sib.get_text(" ", strip=True))
                elif isinstance(sib, NavigableString):
                    after_bits.append(str(sib).strip())
            # Also include content in parent after cut when cut isn't a top-level block
            if not after_bits and cut.parent:
                seen = False
                for child in list(cut.parent.children):
                    if child is cut:
                        seen = True
                        continue
                    if not seen:
                        continue
                    if getattr(child, "get_text", None):
                        after_bits.append(child.get_text(" ", strip=True))
                    elif isinstance(child, NavigableString):
                        after_bits.append(str(child).strip())
            after = " ".join(b for b in after_bits if b)
            # Fallback: whole-document plain after last salutation line
            if not _after_looks_like_sig(after):
                plain = soup.get_text("\n")
                last = None
                for m in re.finditer(
                    r"(?im)^(thanks,?|thank you|best regards|warm regards|"
                    r"kind regards|regards)\s*,?\s*$",
                    plain,
                ):
                    last = m
                if last and _after_looks_like_sig(plain[last.end() :]):
                    after = plain[last.end() :]
                else:
                    return html

            if not _after_looks_like_sig(after):
                return html

            # Drop everything after the salutation element
            parent = cut.parent
            if parent is not None:
                removing = False
                for child in list(parent.children):
                    if child is cut:
                        removing = True
                        continue
                    if removing:
                        try:
                            child.extract()
                        except Exception:
                            pass
            # Also clear following siblings of ancestors (common in Gmail HTML)
            node = cut
            while node is not None and node.name not in (None, "[document]", "body", "html"):
                for sib in list(node.next_siblings):
                    try:
                        sib.extract()
                    except Exception:
                        pass
                node = node.parent
            return str(soup)

        # No salutation element found — plain rebuild if needed
        text = soup.get_text("\n", strip=False)
    except Exception:
        text = re.sub(r"<[^>]+>", "\n", html)

    last = None
    for m in re.finditer(
        r"(?im)^(thanks,?|thank you|best regards|warm regards|kind regards|regards)\s*,?\s*$",
        text,
    ):
        last = m
    if not last or not _after_looks_like_sig(text[last.end() :]):
        return html
    keep_text = text[: last.end()].strip()
    if len(keep_text) < 40:
        return html
    return plain_or_markdown_to_html(keep_text)
