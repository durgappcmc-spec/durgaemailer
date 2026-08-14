# NOTE: Convert markdown / plain text into email-safe HTML; strip residual ** markers.
from __future__ import annotations

import html as _html
import re
from typing import Optional

_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_BOLD_US = re.compile(r"__(.+?)__")
# *text* / *several words* — not * list bullets (space after opening *)
_MD_ITALIC = re.compile(
    r"(?<![\w*])\*(?!\s)([^*\n]{1,240}?)(?<!\s)\*(?![\w*])"
)
_MD_ITALIC_US = re.compile(
    r"(?<![\w_])_(?!\s)([^_\n]{1,240}?)(?<!\s)_(?![\w_])"
)
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


_GREETING_TITLE_PAREN_RE = re.compile(
    r"((?:<(?:p|div)[^>]*>\s*)?(?:Dear|Hi|Hello)\s+[^,<\n(]{1,60}?)\s*\([^)]+\)",
    re.I,
)


def rewrite_opening_greeting(html: str, *, first_name: str = "") -> str:
    """Point the first Dear/Hi/Hello line at this recipient's first name only.

    Never keep a job title in parentheses (e.g. not 'Hi Sushmita (ESG Associate)').
    """
    body = html or ""
    first = (first_name or "").strip()
    if not body:
        return body

    if first:
        def _repl(_m: re.Match) -> str:
            return f"{_m.group(1)}{_m.group(2)}{_m.group(3)}{first}"

        updated, n = _HTML_GREETING_RE.subn(_repl, body, count=1)
        if not n:
            updated, n = _PLAIN_GREETING_RE.subn(_repl, body, count=1)
        if n:
            body = updated

    stripped, n = _GREETING_TITLE_PAREN_RE.subn(r"\1", body, count=1)
    return stripped if n else body


def ensure_designation_in_greeting(
    html: str,
    *,
    first_name: str = "",
    title: str = "",
) -> str:
    """Rewrite the opening greeting to first name only (no title in brackets)."""
    return rewrite_opening_greeting(html or "", first_name=first_name)


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


def _unwrap_emphasis_markers(line: str) -> str:
    """Strip a single wrapping *...* / **...** / _..._ from a whole line."""
    s = (line or "").strip()
    for pat in (
        r"^\*\*(.+?)\*\*:?$",
        r"^__(.+?)__:?$",
        r"^\*(.+?)\*:?$",
        r"^_(.+?)_:?$",
    ):
        m = re.match(pat, s)
        if m:
            inner = m.group(1).strip()
            return inner + (":" if s.endswith(":") and not inner.endswith(":") else "")
    return s


def _markdown_in_text(text: str) -> str:
    """Convert **bold** / *italic* in a plain text run (no HTML tags)."""
    return apply_inline_markdown(text or "", escape_html=False)


def _replace_node_with_html(node, html_fragment: str) -> None:
    """Swap a text node for parsed inline HTML without wrapping <html>/<body>."""
    from bs4 import BeautifulSoup

    frag = BeautifulSoup(f"<span>{html_fragment}</span>", "html.parser")
    span = frag.span
    if span is None or not span.contents:
        node.replace_with(html_fragment)
        return
    children = list(span.contents)
    for child in children:
        node.insert_before(child)
    node.extract()


def _italicize_leftover_in_html(html: str) -> str:
    """Convert leftover *text* markdown in HTML text runs (never inside tags)."""
    if not html or "*" not in html:
        return html
    parts = re.split(r"(<[^>]+>)", html)
    out: list[str] = []
    for part in parts:
        if not part or part.startswith("<"):
            out.append(part)
            continue
        out.append(_markdown_in_text(part))
    return "".join(out)


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
            heading_src = _unwrap_emphasis_markers(line)
            if (
                len(heading_src) < 120
                and not heading_src.endswith(".")
                and not heading_src.lower().startswith("http")
                and (
                    heading_src.endswith(":")
                    or "—" in heading_src
                    or re.match(
                        r"^(The opportunity|Why |What your|See our|Success stories|"
                        r"AI-integrated|Craft &|Full channel|Next step|Thanks,?)",
                        heading_src,
                        re.I,
                    )
                )
            ):
                parts.append(
                    f"<p><strong>{apply_inline_markdown(heading_src)}</strong></p>"
                )
            else:
                parts.append(f"<p>{apply_inline_markdown(line)}</p>")
    return "\n".join(parts)


def render_markdown_in_html(html: str) -> str:
    """Replace leftover **bold** / *italic* inside HTML text nodes."""
    if not html:
        return html or ""
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
            if parent is None or parent.name in (
                "script",
                "style",
                "code",
                "pre",
                "em",
                "strong",
                "i",
                "b",
            ):
                continue
            raw = str(node)
            if "*" not in raw and "_" not in raw:
                continue
            converted = _markdown_in_text(raw)
            if converted == raw:
                continue
            _replace_node_with_html(node, converted)
        rendered = str(soup)
    except Exception:
        rendered = apply_inline_markdown(html, escape_html=False)
    # Catch *text* that survived the tree walk (e.g. odd MIME splits)
    return _italicize_leftover_in_html(rendered)


def normalize_email_html(body: str) -> str:
    """Ensure draft/send body is real HTML with markdown rendered (no raw * / **)."""
    body = (body or "").strip()
    if not body:
        return "<p></p>"
    if re.search(r"</?(?:p|div|br|ul|ol|li|table|strong|em|a|h\d)\b", body, re.I):
        return render_markdown_in_html(body)
    html = plain_or_markdown_to_html(body) or "<p></p>"
    return render_markdown_in_html(html)


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


def clean_email_body(text: str) -> str:
    """Unwrap hard-wrapped prose, collapse double spaces, keep paragraph breaks."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ").replace("\u200b", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    # unwrap single line-breaks inside paragraphs (keep blank lines)
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip() + "\n"


def html_from_cleaned_body(cleaned: str) -> str:
    """Build simple HTML paragraphs from the same cleaned plain text Gmail stores."""
    parts: list[str] = []
    for p in (cleaned or "").split("\n\n"):
        if not p.strip():
            continue
        inner = _html.escape(p.strip()).replace("\n", "<br>")
        parts.append(f"<p>{inner}</p>")
    return "".join(parts) or "<p></p>"


def looks_like_html(body: str) -> bool:
    return bool(
        re.search(r"</?(?:p|div|br|ul|ol|li|table|strong|em|a|h\d)\b", body or "", re.I)
    )


def plain_from_html(html: str) -> str:
    """Extract paragraph-preserving plain text from HTML for clean_email_body()."""
    raw = html or ""
    if not raw.strip():
        return ""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup.find_all(["script", "style"]):
            tag.decompose()
        paras: list[str] = []
        blocks = soup.find_all(["p", "div", "li", "h1", "h2", "h3", "h4", "blockquote"])
        if blocks:
            for el in blocks:
                t = el.get_text(" ", strip=True)
                if t:
                    paras.append(t)
            if paras:
                return "\n\n".join(paras)
        return soup.get_text("\n")
    except Exception:
        text = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
        text = re.sub(r"</p\s*>", "\n\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", "", text)
        return _html.unescape(text)


def prepare_draft_bodies(body: str) -> tuple[str, str]:
    """Return (body_cleaned, html) from LLM/plain/HTML input. Never textwrap.fill()."""
    raw = body or ""
    if looks_like_html(raw):
        plain = plain_from_html(raw)
    else:
        plain = raw
    cleaned = clean_email_body(plain)
    return cleaned, html_from_cleaned_body(cleaned)


_GREETING_LINE_RE = re.compile(
    r"^(Dear|Hi|Hello|Hey|Good\s+(?:morning|afternoon|evening))([^,\n]{0,80})[,:]?",
    re.I,
)
_SIGNOFF_LINE_RE = re.compile(
    r"^(Thanks|Thank you|Best regards|Warm regards|Kind regards|Best|"
    r"Sincerely|Regards|With thanks|Cheers)[,\s]*$",
    re.I,
)


def extract_style_structure(body: str) -> dict[str, str | int]:
    """Paragraph count, words/para, greeting, and sign-off from a sent email body."""
    cleaned = clean_email_body(body or "")
    paras = [p.strip() for p in cleaned.split("\n\n") if p.strip()]
    greeting = ""
    signoff = ""
    if paras:
        first_line = paras[0].split("\n")[0].strip()
        if _GREETING_LINE_RE.match(first_line):
            greeting = first_line[:120]
        else:
            greeting = first_line[:80]
        for i in range(len(paras) - 1, -1, -1):
            head = paras[i].split("\n")[0].strip()
            if _SIGNOFF_LINE_RE.match(head) or (
                i == len(paras) - 1 and len(paras[i].split()) <= 12
            ):
                signoff = "\n".join(paras[i:]).strip()
                if _SIGNOFF_LINE_RE.match(head):
                    break
    n = len(paras)
    words = [len(p.split()) for p in paras] if paras else [0]
    avg = int(round(sum(words) / max(n, 1))) if n else 0
    return {
        "n_paragraphs": n,
        "wc_per_para": avg,
        "greeting": greeting or "Hi,",
        "signoff": signoff or "Best regards,",
    }


def render_draft_html(subject, to, cc, body_cleaned, bcc="", bcc_local=False):
    body_html = "".join(
        f"<p style='margin:0 0 12px 0'>"
        f"{_html.escape(p).replace(chr(10), '<br>')}</p>"
        for p in (body_cleaned or "").split("\n\n")
        if p.strip()
    )
    bcc_line = ""
    if bcc:
        tag = " (local)" if bcc_local else ""
        bcc_line = f"<b>Bcc:</b> {_html.escape(str(bcc))}{tag}<br>"
    return f"""
    <div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;
                line-height:1.5;color:#202124;background:#fff;
                border:1px solid #e0e0e0;border-radius:8px;padding:16px;
                max-width:720px;white-space:normal;">
      <div style="font-size:12px;color:#5f6368;margin-bottom:8px">
        <b>To:</b> {_html.escape(str(to or ""))}<br>
        {f"<b>Cc:</b> {_html.escape(str(cc))}<br>" if cc else ""}
        {bcc_line}
        <b>Subject:</b> {_html.escape(str(subject or ""))}
      </div>
      <hr style="border:none;border-top:1px solid #eee;margin:8px 0 12px 0">
      {body_html}
    </div>
    """
