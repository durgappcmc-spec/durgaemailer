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
    from gmail_client.send import _build_raw_message, default_from_email

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
        svc = gmail_service()
        updated = (
            svc.users()
            .drafts()
            .update(userId="me", id=did, body={"message": {"raw": raw}})
            .execute()
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
    """Send the existing Gmail draft as stored (do not rebuild MIME)."""
    did = (draft_id or "").removeprefix("gmail:")
    if not did:
        return {"error": "missing gmail draft id"}
    try:
        svc = gmail_service()
        sent = svc.users().drafts().send(userId="me", body={"id": did}).execute()
        return {
            "ok": True,
            "message_id": sent.get("id"),
            "thread_id": sent.get("threadId"),
            "gmail_draft_id": did,
        }
    except Exception as e:
        print(f"[gmail] drafts.send failed: {e}", file=sys.stderr)
        return {"error": str(e), "gmail_draft_id": did}


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
    }


def send_gmail_draft(gmail_draft_id: str) -> dict[str, Any]:
    """Send an existing Gmail draft (injects click tracking, preserves open pixel)."""
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
        _update_draft_html(did, draft, body)
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
    """Replace draft MIME with tracked HTML (+ optional attachments)."""
    from gmail_client.send import _build_raw_message

    raw, _tid = _build_raw_message(
        to=draft.get("to") or draft.get("recipient") or "",
        subject=subject if subject is not None else (draft.get("subject") or ""),
        html_body=body_html,
        attachments=attachments or None,
        instrument=False,
        include_signature=False,
        from_email=(draft.get("from") or None),
        cc=draft.get("cc") or None,
        bcc=draft.get("bcc") or None,
        recipient_name=draft.get("recipient_name") or None,
    )
    svc = gmail_service()
    svc.users().drafts().update(
        userId="me",
        id=gmail_draft_id,
        body={"message": {"raw": raw}},
    ).execute()
