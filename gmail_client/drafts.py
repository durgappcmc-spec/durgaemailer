# NOTE: List / fetch / send Gmail drafts (with HTML body decode).
from __future__ import annotations

import base64
import html as _html
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional

from gmail_client.auth import gmail_service

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def normalize_addr_list(s: str) -> str:
    """Split on comma/semicolon, trim, de-dupe case-insensitively, preserve order."""
    if not s:
        return ""
    parts = [a.strip() for a in re.split(r"[,;]", s) if a.strip()]
    seen: set[str] = set()
    out: list[str] = []
    for a in parts:
        k = a.lower()
        if k not in seen:
            seen.add(k)
            out.append(a)
    return ", ".join(out)


def _drop_addrs_already_in_to(field: str, to: str) -> str:
    to_keys = {e.lower() for e in _EMAIL_RE.findall(to or "")}
    if (to or "").strip() and "@" not in (to or ""):
        to_keys.add(to.strip().lower())
    parts = [a.strip() for a in re.split(r"[,;]", field or "") if a.strip()]
    kept: list[str] = []
    for a in parts:
        emails = [e.lower() for e in _EMAIL_RE.findall(a)]
        if emails and all(e in to_keys for e in emails):
            continue
        if a.lower() in to_keys:
            continue
        kept.append(a)
    return normalize_addr_list(", ".join(kept))


def gmail_profile_email() -> str:
    """OAuth account used by this process (`users.getProfile`)."""
    try:
        svc = gmail_service()
        return str(
            (svc.users().getProfile(userId="me").execute() or {}).get(
                "emailAddress"
            )
            or ""
        )
    except Exception as e:
        print(f"[gmail] getProfile failed: {e}", file=sys.stderr)
        return ""


def _decode_b64url(data: str) -> str:
    raw = base64.urlsafe_b64decode((data or "") + "==")
    return raw.decode("utf-8", errors="replace")


def _decode_b64url_bytes(data: str) -> bytes:
    return base64.urlsafe_b64decode((data or "") + "==")


def _mime_base(part: dict) -> str:
    mime = (part.get("mimeType") or "").split(";")[0].strip().lower()
    if mime:
        return mime
    for h in part.get("headers") or []:
        if (h.get("name") or "").lower() == "content-type":
            return (h.get("value") or "").split(";")[0].strip().lower()
    return ""


def _visible_text(html: str, text: str = "") -> str:
    """Visible characters only — Gmail wrapper HTML like <div><br></div> is empty."""
    if (text or "").strip():
        return text.replace("\xa0", " ").strip()
    s = re.sub(r"<br\s*/?>", "\n", html or "", flags=re.I)
    s = re.sub(r"<style\b[^>]*>.*?</style>", " ", s, flags=re.I | re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    return _html.unescape(s).replace("\xa0", " ").strip()


def _bodies_are_blank(html: str, text: str = "") -> bool:
    return not _visible_text(html, text)


def _sniff_text_mime(mime: str, decoded: str) -> str:
    if mime in ("text/html", "text/plain"):
        return mime
    low = (decoded or "")[:500].lower()
    if any(tok in low for tok in ("<html", "<div", "<p>", "<br", "<span", "<table")):
        return "text/html"
    return "text/plain"


def _walk_text_parts(payload: dict, *, msg_id: str = "") -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []

    def walk(part: dict) -> None:
        mime = _mime_base(part)
        body = part.get("body") or {}
        has_data = bool(body.get("data") or body.get("attachmentId"))
        leaf = not part.get("parts")
        if mime in ("text/html", "text/plain") or (
            leaf and has_data and (not mime or mime.startswith("text/"))
        ):
            decoded = _decode_part_text(part, msg_id=msg_id)
            if decoded:
                found.append((_sniff_text_mime(mime, decoded), decoded))
        for child in part.get("parts") or []:
            walk(child)

    walk(payload or {})
    return found


def _best_part(parts: list[str]) -> str:
    scored = [(len(_visible_text(p)), p) for p in parts if (p or "").strip()]
    with_text = [s for s in scored if s[0] > 0]
    if with_text:
        return max(with_text, key=lambda t: t[0])[1]
    return scored[0][1] if scored else ""


def _decode_part_text(part: dict, *, msg_id: str = "") -> str:
    body = part.get("body") or {}
    data = body.get("data")
    if data:
        return _decode_b64url(data)
    att_id = body.get("attachmentId") or ""
    if not att_id or not msg_id:
        return ""
    try:
        svc = gmail_service()
        att = (
            svc.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=msg_id, id=att_id)
            .execute()
        )
        return _decode_b64url(att.get("data") or "")
    except Exception as e:
        print(f"[gmail] part body fetch failed: {e}", file=sys.stderr)
        return ""


def _decode_part_bytes(part: dict, *, msg_id: str = "") -> bytes:
    body = part.get("body") or {}
    data = body.get("data")
    if data:
        try:
            return _decode_b64url_bytes(data)
        except Exception:
            return b""
    att_id = body.get("attachmentId") or ""
    if not att_id or not msg_id:
        return b""
    try:
        svc = gmail_service()
        att = (
            svc.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=msg_id, id=att_id)
            .execute()
        )
        return _decode_b64url_bytes(att.get("data") or "")
    except Exception as e:
        print(f"[gmail] attachment fetch failed: {e}", file=sys.stderr)
        return b""


def extract_gmail_attachments(
    payload: dict, *, msg_id: str = "", max_bytes: int = 20 * 1024 * 1024
) -> list[dict[str, Any]]:
    """File parts from a Gmail payload (skips text/html and text/plain bodies)."""
    out: list[dict[str, Any]] = []
    skip_mime = {
        "",
        "text/html",
        "text/plain",
        "multipart/alternative",
        "multipart/mixed",
        "multipart/related",
        "multipart/signed",
    }

    def walk(part: dict) -> None:
        if not isinstance(part, dict):
            return
        mime = _mime_base(part)
        filename = str(part.get("filename") or "").strip()
        headers = {
            str(h.get("name") or "").lower(): str(h.get("value") or "")
            for h in (part.get("headers") or [])
            if isinstance(h, dict)
        }
        disp = headers.get("content-disposition") or ""
        is_file = bool(filename) or "attachment" in disp.lower()
        if is_file and mime not in skip_mime:
            size_hint = int((part.get("body") or {}).get("size") or 0)
            if size_hint and size_hint > max_bytes:
                print(
                    f"[gmail] skip attachment {filename or mime}: {size_hint} bytes",
                    file=sys.stderr,
                )
            else:
                blob = _decode_part_bytes(part, msg_id=msg_id)
                if blob and len(blob) <= max_bytes:
                    name = filename or "file"
                    out.append(
                        {
                            "name": name,
                            "filename": name,
                            "mime_type": mime or "application/octet-stream",
                            "mimeType": mime or "application/octet-stream",
                            "size": len(blob),
                            "data_base64": base64.b64encode(blob).decode("ascii"),
                        }
                    )
        for child in part.get("parts") or []:
            walk(child)

    walk(payload or {})
    return out


def _payload_hints_files(payload: dict) -> bool:
    """True when Gmail MIME lists a filename/attachment even if bytes were not fetched."""

    def walk(part: dict) -> bool:
        if not isinstance(part, dict):
            return False
        if str(part.get("filename") or "").strip():
            return True
        for h in part.get("headers") or []:
            if not isinstance(h, dict):
                continue
            if (h.get("name") or "").lower() != "content-disposition":
                continue
            if "attachment" in (h.get("value") or "").lower():
                return True
        return any(walk(c) for c in (part.get("parts") or []) if isinstance(c, dict))

    return walk(payload or {})


def _atts_for_gmail_mime(atts: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """{name, data, mime_type} rows for _build_raw_message."""
    out: list[dict[str, Any]] = []
    for a in atts or []:
        if not isinstance(a, dict):
            continue
        data = a.get("data")
        if not data and a.get("data_base64"):
            try:
                data = base64.b64decode(a["data_base64"])
            except Exception:
                data = None
        if not data:
            continue
        if isinstance(data, str):
            data = data.encode("utf-8")
        out.append(
            {
                "name": a.get("name") or a.get("filename") or "file",
                "data": data,
                "mime_type": a.get("mime_type")
                or a.get("mimeType")
                or "application/octet-stream",
            }
        )
    return out


def _extract_plain_body(payload: dict, *, msg_id: str = "") -> str:
    """Prefer text/plain; fall back to stripped text/html. No clean_email_body()."""
    parts = _walk_text_parts(payload, msg_id=msg_id)
    text = _best_part([d for m, d in parts if m == "text/plain"])
    html = _best_part([d for m, d in parts if m == "text/html"])
    if (text or "").strip():
        return text.replace("\r\n", "\n").replace("\r", "\n")
    if html:
        s = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
        s = re.sub(r"</p\s*>", "\n\n", s, flags=re.I)
        s = re.sub(r"<[^>]+>", "", s)
        return _html.unescape(s).replace("\r\n", "\n").replace("\r", "\n")
    return ""


def _extract_html_body(payload: dict, *, msg_id: str = "") -> str:
    """Prefer real text/html; ignore empty Gmail wrappers; else wrap plain text."""
    parts = _walk_text_parts(payload, msg_id=msg_id)
    html = _best_part([d for m, d in parts if m == "text/html"])
    text = _best_part([d for m, d in parts if m == "text/plain"])
    html = (html or "").replace("\r\n", "\n").replace("\r", "\n")
    if not _bodies_are_blank(html, ""):
        return html
    plain = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if plain.strip():
        return _html_from_plain(plain)
    return html


def _extract_from_raw(raw_b64: str) -> tuple[str, str]:
    """Parse RFC822 raw (base64url) into (html, plain)."""
    if not raw_b64:
        return "", ""
    try:
        from email import policy
        from email.parser import BytesParser

        blob = base64.urlsafe_b64decode((raw_b64 or "") + "==")
        msg = BytesParser(policy=policy.default).parsebytes(blob)
        html = ""
        text = ""
        if msg.is_multipart():
            for part in msg.walk():
                ctype = (part.get_content_type() or "").lower()
                if ctype not in ("text/html", "text/plain"):
                    continue
                try:
                    content = part.get_content()
                except Exception:
                    payload = part.get_payload(decode=True) or b""
                    content = payload.decode("utf-8", errors="replace")
                if not isinstance(content, str):
                    content = str(content)
                if ctype == "text/html" and not html:
                    html = content
                elif ctype == "text/plain" and not text:
                    text = content
        else:
            ctype = (msg.get_content_type() or "").lower()
            try:
                content = msg.get_content()
            except Exception:
                payload = msg.get_payload(decode=True) or b""
                content = payload.decode("utf-8", errors="replace")
            if not isinstance(content, str):
                content = str(content)
            if ctype == "text/html":
                html = content
            else:
                text = content
        return html.replace("\r\n", "\n").replace("\r", "\n"), text.replace(
            "\r\n", "\n"
        ).replace("\r", "\n")
    except Exception as e:
        print(f"[gmail] raw MIME parse failed: {e}", file=sys.stderr)
        return "", ""


def _html_from_plain(plain: str) -> str:
    paras = [p for p in (plain or "").split("\n\n") if p.strip()]
    return "".join(
        f"<p>{_html.escape(p).replace(chr(10), '<br>')}</p>" for p in paras
    )


def _defer_open_pixel(
    body_html: str,
    *,
    draft_id: str = "",
    to: str = "",
    cc: str = "",
    bcc: str = "",
    subject: str = "",
    from_email: str = "",
    attachments: Optional[list[dict[str, Any]]] = None,
    skip_persist: bool = False,
) -> str:
    """Strip live open pixels from draft HTML; persist to Gmail when we have a draft id."""
    html = body_html or ""
    if "/.netlify/functions/open" not in html and "/t/o/" not in html:
        return html
    try:
        from core.tracking import prepare_draft_tracking

        clean, _tid = prepare_draft_tracking(html)
    except Exception:
        try:
            from core.tracking import strip_tracking

            clean = strip_tracking(html)
        except Exception:
            return html
    did = (draft_id or "").removeprefix("gmail:")
    if did and not skip_persist:
        try:
            saved = save_gmail_draft(
                did,
                to,
                cc,
                bcc,
                subject,
                clean,
                from_email=from_email or None,
                attachments=_atts_for_gmail_mime(attachments) or None,
            )
            if saved.get("error"):
                print(f"[gmail] strip draft pixel failed: {saved.get('error')}", file=sys.stderr)
            else:
                print(f"[gmail] removed live open pixel from draft {did}", file=sys.stderr)
        except Exception as e:
            print(f"[gmail] strip draft pixel skipped: {e}", file=sys.stderr)
    return clean


def fetch_gmail_draft(draft_id: str) -> dict[str, Any]:
    """Gmail is the source of truth. HTML is primary; plain is derived."""
    did = (draft_id or "").removeprefix("gmail:")
    empty = {
        "id": did,
        "to": "",
        "cc": "",
        "bcc": "",
        "subject": "",
        "body": "",
        "body_html": "",
        "body_text": "",
        "raw_msg": {},
        "source": "gmail_fetch",
        "gmail_draft_id": did,
        "draft_id": f"gmail:{did}" if did else "",
    }
    if not did:
        return {**empty, "error": "missing gmail draft id", "gmail_api_status": "error"}
    try:
        svc = gmail_service()
        d = svc.users().drafts().get(userId="me", id=did, format="full").execute()
        status: Any = 200
    except Exception as e:
        code = getattr(getattr(e, "resp", None), "status", None) or "error"
        print(f"[gmail] drafts.get failed: {e}", file=sys.stderr)
        return {**empty, "error": str(e), "gmail_api_status": code}
    msg = d.get("message") or {}
    payload = msg.get("payload") or {}
    mid = str(msg.get("id") or "")
    headers = {
        h["name"].lower(): h["value"]
        for h in payload.get("headers") or []
    }
    body_html = _extract_html_body(payload, msg_id=mid)
    body_text = _extract_plain_body(payload, msg_id=mid)
    att_payload = payload
    if _bodies_are_blank(body_html, body_text) and mid:
        try:
            svc = gmail_service()
            msg2 = (
                svc.users()
                .messages()
                .get(userId="me", id=mid, format="full")
                .execute()
            )
            payload2 = msg2.get("payload") or {}
            att_payload = payload2 or payload
            body_html = _extract_html_body(payload2, msg_id=mid)
            body_text = _extract_plain_body(payload2, msg_id=mid)
            if not headers:
                headers = {
                    h["name"].lower(): h["value"]
                    for h in payload2.get("headers") or []
                }
            if msg2.get("snippet") and not msg.get("snippet"):
                msg = {**msg, "snippet": msg2.get("snippet")}
        except Exception as e:
            print(f"[gmail] messages.get fallback failed: {e}", file=sys.stderr)
    if _bodies_are_blank(body_html, body_text):
        try:
            svc = gmail_service()
            raw_wrap = (
                svc.users()
                .drafts()
                .get(userId="me", id=did, format="raw")
                .execute()
            )
            raw_b64 = (raw_wrap.get("message") or {}).get("raw") or ""
            html_r, text_r = _extract_from_raw(raw_b64)
            if not _bodies_are_blank(html_r, text_r):
                body_html = html_r or body_html
                body_text = text_r or body_text
        except Exception as e:
            print(f"[gmail] drafts.get raw fallback failed: {e}", file=sys.stderr)
    if _bodies_are_blank(body_html, body_text):
        snip = (msg.get("snippet") or "").strip()
        if snip and _bodies_are_blank("", body_text):
            body_text = snip
        if not _bodies_are_blank("", body_text):
            body_html = _html_from_plain(body_text)
        elif snip:
            body_html = f"<p>{_html.escape(snip)}</p>"
            body_text = snip
    attachments = extract_gmail_attachments(att_payload, msg_id=mid)
    if not attachments and att_payload is not payload:
        attachments = extract_gmail_attachments(payload, msg_id=mid)
    hinted_files = _payload_hints_files(att_payload) or _payload_hints_files(payload)
    body_html = _defer_open_pixel(
        body_html,
        draft_id=did,
        to=headers.get("to", ""),
        cc=headers.get("cc", ""),
        bcc=headers.get("bcc", ""),
        subject=headers.get("subject", ""),
        from_email=headers.get("from", ""),
        attachments=attachments,
        skip_persist=bool(hinted_files and not attachments),
    )
    return {
        "id": did,
        "to": headers.get("to", ""),
        "cc": headers.get("cc", ""),
        "bcc": headers.get("bcc", ""),
        "subject": headers.get("subject", ""),
        "body": body_text,
        "body_html": body_html,
        "body_text": body_text,
        "snippet": msg.get("snippet") or "",
        "raw_msg": msg,
        "gmail_api_status": status,
        "source": "gmail_fetch",
        "gmail_draft_id": did,
        "draft_id": f"gmail:{did}",
        "attachments": attachments,
    }


def fetch_gmail_message(message_id: str) -> dict[str, Any]:
    """Load a Gmail Drafts-folder message when users.drafts has no draft id."""
    mid = (message_id or "").strip()
    empty = {
        "id": "",
        "to": "",
        "cc": "",
        "bcc": "",
        "subject": "",
        "body": "",
        "body_html": "",
        "body_text": "",
        "raw_msg": {},
        "source": "gmail_fetch",
        "gmail_draft_id": "",
        "gmail_message_id": mid,
        "draft_id": f"gmail-msg:{mid}" if mid else "",
    }
    if not mid:
        return {**empty, "error": "missing gmail message id", "gmail_api_status": "error"}
    try:
        svc = gmail_service()
        msg = svc.users().messages().get(userId="me", id=mid, format="full").execute()
        status: Any = 200
    except Exception as e:
        code = getattr(getattr(e, "resp", None), "status", None) or "error"
        print(f"[gmail] messages.get failed: {e}", file=sys.stderr)
        return {**empty, "error": str(e), "gmail_api_status": code}
    payload = msg.get("payload") or {}
    headers = {
        h["name"].lower(): h["value"]
        for h in payload.get("headers") or []
    }
    body_html = _extract_html_body(payload, msg_id=mid)
    body_text = _extract_plain_body(payload, msg_id=mid)
    if _bodies_are_blank(body_html, body_text):
        try:
            svc = gmail_service()
            raw_wrap = (
                svc.users()
                .messages()
                .get(userId="me", id=mid, format="raw")
                .execute()
            )
            html_r, text_r = _extract_from_raw(raw_wrap.get("raw") or "")
            if not _bodies_are_blank(html_r, text_r):
                body_html = html_r or body_html
                body_text = text_r or body_text
        except Exception as e:
            print(f"[gmail] messages.get raw fallback failed: {e}", file=sys.stderr)
    if _bodies_are_blank(body_html, body_text):
        snip = (msg.get("snippet") or "").strip()
        if snip:
            body_text = snip
            body_html = _html_from_plain(snip)
    try:
        from core.tracking import html_for_preview

        body_html = html_for_preview(body_html)
    except Exception:
        pass
    return {
        "id": "",
        "to": headers.get("to", ""),
        "cc": headers.get("cc", ""),
        "bcc": headers.get("bcc", ""),
        "subject": headers.get("subject", ""),
        "body": body_text,
        "body_html": body_html,
        "body_text": body_text,
        "snippet": msg.get("snippet") or "",
        "raw_msg": msg,
        "gmail_api_status": status,
        "source": "gmail_fetch",
        "gmail_draft_id": "",
        "gmail_message_id": mid,
        "draft_id": f"gmail-msg:{mid}",
        "attachments": extract_gmail_attachments(payload, msg_id=mid),
    }


def save_gmail_draft(
    draft_id: str,
    to: str,
    cc: str,
    bcc: str,
    subject: str,
    body: str,
    *,
    attachments: Optional[list[dict[str, Any]]] = None,
    from_email: Optional[str] = None,
) -> dict[str, Any]:
    """Write HTML + text/plain to Gmail. `body` may be HTML or plain."""
    from gmail_client.html_format import (
        clean_email_body,
        html_from_cleaned_body,
        looks_like_html,
        plain_from_html,
        sanitize_email_html,
    )
    from gmail_client.send import _build_raw_message, default_from_email, put_gmail_draft_raw

    did = (draft_id or "").removeprefix("gmail:")
    if not did:
        return {"error": "missing gmail draft id"}
    raw_body = body or ""
    if looks_like_html(raw_body):
        html = sanitize_email_html(raw_body)
        cleaned = clean_email_body(plain_from_html(html))
    else:
        cleaned = clean_email_body(raw_body)
        html = html_from_cleaned_body(cleaned)
    to_n = normalize_addr_list(to)
    cc_n = _drop_addrs_already_in_to(normalize_addr_list(cc), to_n)
    bcc_n = _drop_addrs_already_in_to(normalize_addr_list(bcc), to_n)
    try:
        from core.tracking import prepare_draft_tracking

        html, _tid = prepare_draft_tracking(html)
    except Exception:
        pass
    try:
        raw, _tid = _build_raw_message(
            to=to_n,
            subject=subject or "",
            html_body=html,
            plain_body=cleaned,
            attachments=attachments or None,
            instrument=False,
            include_signature=False,
            from_email=from_email or default_from_email(),
            cc=cc_n or None,
            bcc=bcc_n or None,
        )
        updated = put_gmail_draft_raw(
            raw,
            draft_id=did,
            use_media=bool(attachments),
        )
        return {
            "ok": True,
            "gmail_draft_id": did,
            "to": to_n,
            "cc": cc_n,
            "bcc": bcc_n,
            "subject": subject or "",
            "body_cleaned": cleaned,
            "body_html": html,
            "updated": updated,
        }
    except Exception as e:
        print(f"[gmail] drafts.update failed: {e}", file=sys.stderr)
        return {"error": str(e), "gmail_draft_id": did}


def send_draft(draft_id: str) -> dict[str, Any]:
    """Send a Gmail draft, injecting the open pixel only at send time."""
    return send_gmail_draft(draft_id)


def _collect_draft_refs(list_page, *, limit: int = 200) -> list[tuple[str, str]]:
    """Walk drafts.list pages. list_page(page_token) -> API dict. Returns [(draft_id, message_id)]."""
    cap = min(max(int(limit), 1), 500)
    refs: list[tuple[str, str]] = []
    page_token: str | None = None
    seen: set[str] = set()
    while len(refs) < cap:
        res = list_page(page_token) or {}
        for row in res.get("drafts") or []:
            did = str(row.get("id") or "").strip()
            if not did or did in seen:
                continue
            seen.add(did)
            mid = str((row.get("message") or {}).get("id") or "").strip()
            refs.append((did, mid))
            if len(refs) >= cap:
                break
        page_token = res.get("nextPageToken") or None
        if not page_token:
            break
    return refs


def _collect_draft_folder_message_ids(list_page, *, limit: int = 500) -> list[str]:
    """Walk messages.list q=in:drafts. list_page(page_token) -> API dict."""
    cap = min(max(int(limit), 1), 500)
    ids: list[str] = []
    seen: set[str] = set()
    page_token: str | None = None
    while len(ids) < cap:
        res = list_page(page_token) or {}
        for row in res.get("messages") or []:
            mid = str(row.get("id") or "").strip()
            if not mid or mid in seen:
                continue
            seen.add(mid)
            ids.append(mid)
            if len(ids) >= cap:
                break
        page_token = res.get("nextPageToken") or None
        if not page_token:
            break
    return ids


def list_gmail_drafts(limit: int = 200) -> list[dict[str, Any]]:
    """Return Gmail Drafts-folder rows. Paginates; never drops a listed draft."""
    try:
        svc = gmail_service()

        def _drafts_page(token: str | None) -> dict:
            kwargs: dict[str, Any] = {
                "userId": "me",
                "maxResults": min(100, max(int(limit), 1)),
            }
            if token:
                kwargs["pageToken"] = token
            return svc.users().drafts().list(**kwargs).execute()

        refs = _collect_draft_refs(_drafts_page, limit=limit)
    except Exception as e:
        print(f"[gmail] list drafts failed: {e}", file=sys.stderr)
        return [{"error": str(e)}]

    if not refs:
        # Still check the Drafts *label* — Gmail web can show messages
        # that have not yet appeared in users.drafts.list.
        refs = []

    out_by_id: dict[str, dict[str, Any]] = {}

    def _one(did: str, mid: str) -> tuple[str, dict[str, Any]]:
        meta = _draft_headers(did, format_="metadata") if did else {}
        if (not (meta.get("subject") or meta.get("to"))) and mid:
            mh = _message_headers(mid)
            meta = {**meta, **{k: v for k, v in mh.items() if v}}
        message_id = meta.get("message_id") or mid
        return did, {
            "draft_id": f"gmail:{did}" if did else f"gmail-msg:{message_id}",
            "gmail_draft_id": did,
            "gmail_message_id": message_id,
            "recipient": meta.get("to") or "",
            "to": meta.get("to") or "",
            "cc": meta.get("cc") or "",
            "bcc": meta.get("bcc") or "",
            "subject": meta.get("subject") or "(no subject)",
            "snippet": meta.get("snippet") or "",
            "updated_at": meta.get("internal_date") or "",
            "status": "draft",
            "source": "gmail",
            "tracking_id": meta.get("tracking_id") or "",
            "has_open_pixel": bool(meta.get("has_open_pixel")),
        }

    workers = min(8, max(1, len(refs)))
    if refs:
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(_one, did, mid) for did, mid in refs]
                for fut in as_completed(futures):
                    try:
                        did, row = fut.result()
                        key = did or row.get("draft_id") or ""
                        out_by_id[str(key)] = row
                    except Exception as e:
                        print(f"[gmail] draft meta failed: {e}", file=sys.stderr)
        except Exception as e:
            print(f"[gmail] parallel draft list failed: {e}", file=sys.stderr)

        # Guarantee every listed draft id appears even if a worker threw.
        for did, mid in refs:
            if did in out_by_id:
                continue
            if f"gmail:{did}" in {r.get("draft_id") for r in out_by_id.values()}:
                continue
            try:
                _, row = _one(did, mid)
                out_by_id[did] = row
            except Exception:
                out_by_id[did] = {
                    "draft_id": f"gmail:{did}",
                    "gmail_draft_id": did,
                    "gmail_message_id": mid,
                    "recipient": "",
                    "to": "",
                    "subject": "(gmail draft)",
                    "status": "draft",
                    "source": "gmail",
                }

    # Gmail web Drafts folder (label) — add any message not already mapped.
    try:
        svc = gmail_service()

        def _msgs_page(token: str | None) -> dict:
            kwargs: dict[str, Any] = {
                "userId": "me",
                "q": "in:drafts",
                "maxResults": 100,
            }
            if token:
                kwargs["pageToken"] = token
            return svc.users().messages().list(**kwargs).execute()

        folder_ids = _collect_draft_folder_message_ids(_msgs_page, limit=max(int(limit), 200))
        known_mids = {
            str(r.get("gmail_message_id") or "")
            for r in out_by_id.values()
        }
        for mid in folder_ids:
            if not mid or mid in known_mids:
                continue
            meta = _message_headers(mid)
            row = {
                "draft_id": f"gmail-msg:{mid}",
                "gmail_draft_id": "",
                "gmail_message_id": mid,
                "recipient": meta.get("to") or "",
                "to": meta.get("to") or "",
                "cc": meta.get("cc") or "",
                "bcc": meta.get("bcc") or "",
                "subject": meta.get("subject") or "(no subject)",
                "snippet": meta.get("snippet") or "",
                "updated_at": meta.get("internal_date") or "",
                "status": "draft",
                "source": "gmail_folder",
                "tracking_id": "",
                "has_open_pixel": False,
            }
            out_by_id[f"msg:{mid}"] = row
            known_mids.add(mid)
    except Exception as e:
        print(f"[gmail] in:drafts folder list failed: {e}", file=sys.stderr)

    rows = list(out_by_id.values())
    rows.sort(key=lambda r: str(r.get("updated_at") or ""), reverse=True)
    return rows


def draft_identity_keys(row: dict[str, Any]) -> set[str]:
    """Stable ids used to match a Gmail draft to its Drive metadata row."""
    keys: set[str] = set()
    did = str(row.get("draft_id") or "").strip()
    gid = str(row.get("gmail_draft_id") or "").strip().removeprefix("gmail:")
    mid = str(row.get("gmail_message_id") or "").strip()
    if mid.startswith("gmail-msg:"):
        mid = mid.removeprefix("gmail-msg:")
    if did:
        keys.add(did)
        if did.startswith("gmail:"):
            keys.add(did.removeprefix("gmail:"))
        elif did.startswith("gmail-msg:"):
            keys.add(did.removeprefix("gmail-msg:"))
            keys.add(did)
        else:
            keys.add(f"gmail:{did}")
    if gid:
        keys.add(gid)
        keys.add(f"gmail:{gid}")
    if mid:
        keys.add(mid)
        keys.add(f"gmail-msg:{mid}")
    return {k for k in keys if k}


def _drive_row_had_gmail(row: dict[str, Any]) -> bool:
    did = str(row.get("draft_id") or "")
    gid = str(row.get("gmail_draft_id") or "").strip()
    mid = str(row.get("gmail_message_id") or "").strip()
    return bool(
        gid
        or mid
        or did.startswith("gmail:")
        or did.startswith("gmail-msg:")
    )


def merge_gmail_and_drive_drafts(
    gmail_rows: list[dict[str, Any]],
    drive_rows: list[dict[str, Any]],
    *,
    gmail_ok: bool = True,
) -> list[dict[str, Any]]:
    """Gmail Drafts folder is the list of emails still to send.

    Drive only enriches matching live Gmail rows (title, tracking). A Drive
    copy of a Gmail draft that is no longer in Drafts (already sent or
    discarded) is hidden. True Drive-only fallbacks (never reached Gmail)
    stay visible so a failed create is not lost.
    """
    by_primary: dict[str, dict[str, Any]] = {}
    key_to_primary: dict[str, str] = {}

    def _index(row: dict[str, Any], primary: str) -> None:
        by_primary[primary] = row
        for k in draft_identity_keys(row):
            key_to_primary[k] = primary

    for r in gmail_rows or []:
        if r.get("error"):
            continue
        primary = str(r.get("draft_id") or "").strip()
        if not primary:
            gid = str(r.get("gmail_draft_id") or "").strip()
            primary = f"gmail:{gid}" if gid else ""
        if not primary:
            continue
        _index(
            {**r, "origin": "gmail", "status": r.get("status") or "draft"},
            primary,
        )

    leftovers: list[dict[str, Any]] = []
    for r in drive_rows or []:
        keys = draft_identity_keys(r)
        hit = next((key_to_primary[k] for k in keys if k in key_to_primary), None)
        if hit:
            cur = by_primary[hit]
            if not cur.get("tracking_id") and r.get("tracking_id"):
                cur["tracking_id"] = r["tracking_id"]
            cur["has_open_pixel"] = cur.get("has_open_pixel") or r.get(
                "has_open_pixel"
            )
            for extra in (
                "title",
                "designation",
                "company",
                "recipient_name",
                "bulk_job_id",
            ):
                if not cur.get(extra) and r.get(extra):
                    cur[extra] = r[extra]
            cur["origin"] = "drive+gmail"
            continue
        leftovers.append(r)

    for r in leftovers:
        status = str(r.get("status") or "draft").lower()
        if status in ("sent", "deleted"):
            continue
        did = str(r.get("draft_id") or "").strip()
        if not did:
            continue
        if gmail_ok and _drive_row_had_gmail(r):
            # Gmail Drafts no longer has this message → already sent/removed
            continue
        _index({**r, "origin": r.get("source") or "drive"}, did)

    return list(by_primary.values())


def mark_drive_draft_sent(
    *,
    gmail_draft_id: str = "",
    draft_id: str = "",
) -> None:
    """Flip Drive index status so a sent Gmail draft cannot reappear as to-send."""
    try:
        from core import drive_db
    except Exception:
        return
    ids: list[str] = []
    if draft_id:
        ids.append(str(draft_id).strip())
    gid = str(gmail_draft_id or "").strip().removeprefix("gmail:")
    if gid:
        ids.append(f"gmail:{gid}")
        ids.append(gid)
    seen: set[str] = set()
    for did in ids:
        if not did or did in seen:
            continue
        seen.add(did)
        try:
            d = drive_db.load_draft(did)
        except Exception:
            continue
        d["status"] = "sent"
        try:
            drive_db.save_draft(did, d)
        except Exception as e:
            print(f"[gmail] mark drive sent skipped {did}: {e}", file=sys.stderr)


def get_gmail_draft(gmail_draft_id: str) -> dict[str, Any]:
    """Full draft. HTML is primary; text/plain is not re-cleaned on read."""
    fetched = fetch_gmail_draft(gmail_draft_id)
    did = fetched.get("id") or (gmail_draft_id or "").removeprefix("gmail:")
    if fetched.get("error"):
        return {
            "error": fetched["error"],
            "gmail_draft_id": did,
            "gmail_api_status": fetched.get("gmail_api_status"),
            "source": "gmail_fetch",
        }
    msg = fetched.get("raw_msg") or {}
    html, text = _extract_bodies(msg.get("payload") or {})
    body_html = fetched.get("body_html") or html or ""
    body = fetched.get("body_text") or fetched.get("body") or text or ""
    if not body_html:
        body_html = html or (f"<pre>{text}</pre>" if text else "")
    tracking_id = _extract_tracking_id(body_html)
    to = fetched.get("to") or ""
    to_email = (_EMAIL_RE.findall(to) or [to])[0] if to else ""
    headers = {
        h["name"].lower(): h["value"]
        for h in (msg.get("payload") or {}).get("headers") or []
    }
    return {
        "draft_id": f"gmail:{did}",
        "gmail_draft_id": did,
        "gmail_message_id": msg.get("id") or "",
        "to": to,
        "recipient": to_email or to,
        "cc": fetched.get("cc") or "",
        "bcc": fetched.get("bcc") or "",
        "subject": fetched.get("subject") or "(no subject)",
        "body": body,
        "body_html": body_html,
        "body_text": body,
        "body_cleaned": body,
        "snippet": msg.get("snippet") or "",
        "status": "draft",
        "source": fetched.get("source") or "gmail_fetch",
        "gmail_api_status": fetched.get("gmail_api_status") or 200,
        "tracking_id": tracking_id or "",
        "has_open_pixel": bool(tracking_id)
        or "/.netlify/functions/open" in (body_html or "")
        or "/t/o/" in (body_html or ""),
        "from": headers.get("from") or "",
        "attachments": fetched.get("attachments") or [],
    }


def _replace_html_in_rfc822(raw_b64: str, new_html: str) -> str:
    """Swap the body text/html part; leave attachment siblings untouched."""
    if not raw_b64 or new_html is None:
        return ""
    try:
        from email import policy
        from email.parser import BytesParser
        from email.policy import SMTP
    except Exception:
        return ""
    try:
        blob = base64.urlsafe_b64decode((raw_b64 or "") + "==")
        msg = BytesParser(policy=policy.default).parsebytes(blob)
        replaced = False
        for part in msg.walk():
            if part.get_content_type() != "text/html":
                continue
            if part.get_filename():
                continue
            if (part.get_content_disposition() or "").lower() == "attachment":
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                part.set_content(new_html, subtype="html", charset=charset)
            except Exception:
                if "Content-Transfer-Encoding" in part:
                    del part["Content-Transfer-Encoding"]
                part.set_payload(new_html.encode(charset, errors="replace"))
                try:
                    part.set_charset(charset)
                except Exception:
                    pass
            replaced = True
            break
        if not replaced:
            return ""
        out = msg.as_bytes(policy=SMTP)
        return base64.urlsafe_b64encode(out).decode("ascii").rstrip("=")
    except Exception as e:
        print(f"[gmail] replace html in rfc822 failed: {e}", file=sys.stderr)
        return ""


def _load_gmail_attachments(gmail_draft_id: str) -> list[dict[str, Any]]:
    """File parts currently on the Gmail draft, ready for MIME rebuild."""
    did = (gmail_draft_id or "").removeprefix("gmail:")
    if not did:
        return []
    try:
        svc = gmail_service()
        wrap = svc.users().drafts().get(userId="me", id=did, format="full").execute()
        msg = wrap.get("message") or {}
        payload = msg.get("payload") or {}
        mid = str(msg.get("id") or "")
        return _atts_for_gmail_mime(
            extract_gmail_attachments(payload, msg_id=mid)
        )
    except Exception as e:
        print(f"[gmail] load draft attachments failed: {e}", file=sys.stderr)
        return []


def _gmail_draft_hints_files(gmail_draft_id: str) -> bool:
    did = (gmail_draft_id or "").removeprefix("gmail:")
    if not did:
        return False
    try:
        svc = gmail_service()
        wrap = svc.users().drafts().get(userId="me", id=did, format="full").execute()
        payload = (wrap.get("message") or {}).get("payload") or {}
        return _payload_hints_files(payload)
    except Exception:
        return False


def _attachments_for_draft_update(
    draft: dict[str, Any] | None,
    gmail_draft_id: str,
) -> list[dict[str, Any]]:
    atts = _atts_for_gmail_mime((draft or {}).get("attachments"))
    if atts:
        return atts
    return _load_gmail_attachments(gmail_draft_id)


def _patch_gmail_draft_html(gmail_draft_id: str, body_html: str) -> bool:
    """Rewrite only the HTML part of an existing Gmail draft (keeps files)."""
    did = (gmail_draft_id or "").removeprefix("gmail:")
    if not did:
        return False
    try:
        from gmail_client.send import put_gmail_draft_raw

        svc = gmail_service()
        wrap = svc.users().drafts().get(userId="me", id=did, format="raw").execute()
        msg = wrap.get("message") or {}
        new_raw = _replace_html_in_rfc822(msg.get("raw") or "", body_html)
        if not new_raw:
            return False
        put_gmail_draft_raw(
            new_raw,
            draft_id=did,
            thread_id=str(msg.get("threadId") or ""),
            use_media=True,
        )
        return True
    except Exception as e:
        print(f"[gmail] html patch before send failed: {e}", file=sys.stderr)
        return False


def send_gmail_draft(gmail_draft_id: str) -> dict[str, Any]:
    """Send an existing Gmail draft (injects click + open tracking at send time)."""
    did = (gmail_draft_id or "").removeprefix("gmail:")
    draft = get_gmail_draft(did)
    if draft.get("error"):
        return draft
    body = draft.get("body_html") or ""
    tid = draft.get("tracking_id") or ""
    try:
        from core.tracking import inject_tracking

        # At send time: wrap clicks + keep/add open pixel (Netlify URLs OK in sent mail)
        body, tid = inject_tracking(
            body,
            tracking_id=tid or None,
            recipient_email=draft.get("to") or "",
            subject=draft.get("subject") or "",
            register=True,
            track_clicks=True,
            track_opens=True,
        )
        # Prefer in-place HTML patch so Gmail file parts are not rebuilt away.
        patched = _patch_gmail_draft_html(did, body)
        if not patched:
            atts = _attachments_for_draft_update(draft, did)
            if atts or not _gmail_draft_hints_files(did):
                _update_draft_html(did, draft, body, attachments=atts or None)
            else:
                print(
                    "[gmail] skip html rewrite before send; keep existing MIME with files",
                    file=sys.stderr,
                )
        draft["body_html"] = body
        draft["tracking_id"] = tid
        draft["has_open_pixel"] = True
    except Exception as e:
        print(f"[gmail] tracking inject before send failed: {e}", file=sys.stderr)

    try:
        svc = gmail_service()
        sent = (
            svc.users()
            .drafts()
            .send(userId="me", body={"id": did})
            .execute()
        )
        mark_drive_draft_sent(
            gmail_draft_id=did,
            draft_id=str(draft.get("draft_id") or ""),
        )
        return {
            "ok": True,
            "message_id": sent.get("id"),
            "thread_id": sent.get("threadId"),
            "tracking_id": tid,
            "tracked": bool(tid),
            "gmail_draft_id": did,
            "to": draft.get("to"),
            "subject": draft.get("subject"),
        }
    except Exception as e:
        print(f"[gmail] send draft failed: {e}", file=sys.stderr)
        return {"error": str(e), "gmail_draft_id": did, "tracking_id": tid}


def gmail_delete_refs(
    draft_id: str = "",
    row: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Return (gmail_draft_id, gmail_message_id) for a Drafts-page row."""
    row = row or {}
    did = str(draft_id or row.get("draft_id") or "")
    gmail_id = str(row.get("gmail_draft_id") or "").strip()
    if not gmail_id and did.startswith("gmail:"):
        gmail_id = did.removeprefix("gmail:")
    mid = str(row.get("gmail_message_id") or "").strip()
    if not mid and did.startswith("gmail-msg:"):
        mid = did.removeprefix("gmail-msg:")
    return gmail_id, mid


def delete_gmail_item(
    *,
    gmail_draft_id: str = "",
    gmail_message_id: str = "",
) -> dict[str, Any]:
    """Delete a Gmail draft, or trash a Drafts-folder message if there is no draft id."""
    did = (gmail_draft_id or "").removeprefix("gmail:").strip()
    mid = (gmail_message_id or "").removeprefix("gmail-msg:").strip()
    if did:
        result = delete_gmail_draft(did)
        if result.get("ok") or not mid:
            return result
    if not mid:
        return {"error": "missing gmail draft or message id"}
    try:
        svc = gmail_service()
        svc.users().messages().trash(userId="me", id=mid).execute()
        return {"ok": True, "gmail_message_id": mid, "trashed": True}
    except Exception as e:
        print(f"[gmail] trash draft message failed: {e}", file=sys.stderr)
        return {"error": str(e), "gmail_message_id": mid}


def delete_gmail_draft(gmail_draft_id: str) -> dict[str, Any]:
    """Permanently delete a Gmail draft (users.drafts.delete)."""
    did = (gmail_draft_id or "").removeprefix("gmail:")
    if not did:
        return {"error": "missing gmail draft id"}
    try:
        svc = gmail_service()
        svc.users().drafts().delete(userId="me", id=did).execute()
        return {"ok": True, "gmail_draft_id": did}
    except Exception as e:
        print(f"[gmail] delete draft failed: {e}", file=sys.stderr)
        return {"error": str(e), "gmail_draft_id": did}


def _internal_date(msg: dict) -> str:
    internal = msg.get("internalDate")
    if not internal:
        return ""
    try:
        from datetime import datetime, timezone

        return (
            datetime.fromtimestamp(int(internal) / 1000.0, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except Exception:
        return str(internal)


def _headers_from_message(msg: dict) -> dict[str, Any]:
    payload = msg.get("payload") or {}
    headers = {
        h["name"].lower(): h["value"]
        for h in payload.get("headers") or []
    }
    to = headers.get("to") or ""
    to_email = (_EMAIL_RE.findall(to) or [to])[0] if to else ""
    return {
        "to": to_email,
        "cc": headers.get("cc") or "",
        "bcc": headers.get("bcc") or "",
        "subject": headers.get("subject") or "",
        "snippet": msg.get("snippet") or "",
        "message_id": msg.get("id") or "",
        "internal_date": _internal_date(msg),
        "tracking_id": "",
        "has_open_pixel": False,
    }


def _message_headers(message_id: str) -> dict[str, Any]:
    mid = (message_id or "").strip()
    if not mid:
        return {}
    try:
        svc = gmail_service()
        msg = (
            svc.users()
            .messages()
            .get(
                userId="me",
                id=mid,
                format="metadata",
                metadataHeaders=["To", "Cc", "Bcc", "Subject", "From"],
            )
            .execute()
        )
        return _headers_from_message(msg)
    except Exception as e:
        print(f"[gmail] messages.get metadata failed: {e}", file=sys.stderr)
        return {}


def _draft_headers(
    gmail_draft_id: str, *, format_: str = "metadata"
) -> dict[str, Any]:
    try:
        svc = gmail_service()
        # drafts.get does not accept metadataHeaders — that param is messages.get only
        # and caused empty To/Subject on the Drafts list.
        full = (
            svc.users()
            .drafts()
            .get(userId="me", id=gmail_draft_id, format=format_ or "metadata")
            .execute()
        )
    except Exception:
        return {}
    msg = full.get("message") or {}
    out = _headers_from_message(msg)
    html = ""
    if format_ == "full":
        html, _text = _extract_bodies(msg.get("payload") or {})
        tid = _extract_tracking_id(html or "") if html else None
        out["tracking_id"] = tid or ""
        out["has_open_pixel"] = bool(tid) or "/.netlify/functions/open" in (
            html or ""
        ) or "/t/o/" in (html or "")
    return out


def _extract_bodies(payload: dict, *, msg_id: str = "") -> tuple[str, str]:
    return (
        _extract_html_body(payload, msg_id=msg_id),
        _extract_plain_body(payload, msg_id=msg_id),
    )


def _extract_tracking_id(html: str) -> Optional[str]:
    if not html:
        return None
    try:
        from core.tracking import extract_tracking_id

        return extract_tracking_id(html)
    except Exception:
        m = re.search(
            r"(?:/\.netlify/functions/open|/t/o/)(?:\?id=)?([0-9a-fA-F\-]{8,})",
            html,
        )
        return m.group(1) if m else None


def _update_draft_html(
    gmail_draft_id: str,
    draft: dict,
    body_html: str,
    *,
    attachments: Optional[list[dict[str, Any]]] = None,
    subject: Optional[str] = None,
) -> None:
    """Replace draft MIME with tracked HTML. None attachments keeps existing files."""
    from gmail_client.send import _build_raw_message, put_gmail_draft_raw

    atts = attachments
    if atts is None:
        atts = _attachments_for_draft_update(draft, gmail_draft_id)
    raw, _tid = _build_raw_message(
        to=draft.get("to") or draft.get("recipient") or "",
        subject=subject if subject is not None else (draft.get("subject") or ""),
        html_body=body_html,
        attachments=atts or None,
        instrument=False,
        include_signature=False,
        from_email=(draft.get("from") or None),
        cc=draft.get("cc") or None,
        bcc=draft.get("bcc") or None,
        recipient_name=draft.get("recipient_name") or None,
    )
    put_gmail_draft_raw(
        raw,
        draft_id=gmail_draft_id,
        use_media=bool(atts),
    )
