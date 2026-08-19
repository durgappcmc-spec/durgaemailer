# NOTE: PDF library matching + per-email attach resolution.
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class _Up:
    def __init__(self, name: str, data: bytes, mime: str = "application/pdf"):
        self.name = name
        self._data = data
        self.type = mime

    def getvalue(self) -> bytes:
        return self._data


def test_save_list_delete_and_match(tmp_path, monkeypatch):
    from core import pdf_library as lib

    monkeypatch.setattr(lib, "_LIB_DIR", tmp_path)
    blob = b"%PDF-fake-one-pager%"
    saved = lib.save_uploads([_Up("one-pager.pdf", blob)])
    assert saved and saved[0]["name"] == "one-pager.pdf"
    rows = lib.list_pdfs()
    assert len(rows) == 1
    hits = lib.match_query("one-pager", rows)
    assert hits and hits[0]["name"] == "one-pager.pdf"
    hits = lib.match_query("one-pager.pdf", rows)
    assert hits and hits[0]["id"] == rows[0]["id"]
    loaded = lib.load_attachment(rows[0])
    assert loaded and loaded["data"] == blob
    assert lib.delete_pdf(rows[0]["id"]) is True
    assert lib.list_pdfs() == []


def test_files_mentioned_by_stem(tmp_path, monkeypatch):
    from core import pdf_library as lib

    monkeypatch.setattr(lib, "_LIB_DIR", tmp_path)
    lib.save_uploads(
        [
            _Up("CSR-brochure.pdf", b"%PDF-b%"),
            _Up("rate-card.pdf", b"%PDF-r%"),
        ]
    )
    rows = lib.list_pdfs()
    found = lib.files_mentioned(
        "draft to jane@acme.com and attach the CSR-brochure",
        rows,
    )
    names = [r["name"] for r in found]
    assert names == ["CSR-brochure.pdf"]
    assert lib.files_mentioned("just chatting", rows) == []


def test_resolve_per_email_library_pdfs(tmp_path, monkeypatch):
    from core import pdf_library as lib
    from core.style_draft import parse_directives

    monkeypatch.setattr(lib, "_LIB_DIR", tmp_path)
    lib.save_uploads(
        [
            _Up("one-pager.pdf", b"%PDF-1%"),
            _Up("deck.pdf", b"%PDF-2%"),
        ]
    )
    msg = (
        "draft to jane@acme.com attach one-pager.pdf "
        "and to bob@y.com attach deck.pdf"
    )
    d = parse_directives(msg)
    assert d["attachments_by_email"]["jane@acme.com"]
    assert "one-pager.pdf" in d["attachments_by_email"]["jane@acme.com"][0].lower()
    assert "deck.pdf" in d["attachments_by_email"]["bob@y.com"][0].lower()
    resolved = lib.resolve_message_attachments(
        msg,
        named=d["attachments"],
        named_by_email=d["attachments_by_email"],
        wants_attach=True,
    )
    jane = [a["name"] for a in resolved["by_email"]["jane@acme.com"]]
    bob = [a["name"] for a in resolved["by_email"]["bob@y.com"]]
    assert jane == ["one-pager.pdf"]
    assert bob == ["deck.pdf"]
    picked = lib.pick_for_recipient(
        "jane@acme.com",
        default=resolved["default"],
        by_email=resolved["by_email"],
    )
    assert [a["name"] for a in (picked or [])] == ["one-pager.pdf"]
    bob_picked = lib.pick_for_recipient(
        "bob@y.com",
        default=resolved["default"],
        by_email=resolved["by_email"],
    )
    assert [a["name"] for a in (bob_picked or [])] == ["deck.pdf"]


def test_resolve_global_attach_applies_to_everyone(tmp_path, monkeypatch):
    from core import pdf_library as lib
    from core.style_draft import parse_directives

    monkeypatch.setattr(lib, "_LIB_DIR", tmp_path)
    lib.save_uploads([_Up("brochure.pdf", b"%PDF-b%")])
    msg = "draft to jane@acme.com and bob@y.com attach brochure.pdf"
    d = parse_directives(msg)
    resolved = lib.resolve_message_attachments(
        msg,
        named=d["attachments"],
        named_by_email=d["attachments_by_email"],
        wants_attach=True,
    )
    # Same file on both recipients (clause-level assign) or as default
    jane = lib.pick_for_recipient(
        "jane@acme.com",
        default=resolved["default"],
        by_email=resolved["by_email"],
    )
    bob = lib.pick_for_recipient(
        "bob@y.com",
        default=resolved["default"],
        by_email=resolved["by_email"],
    )
    assert jane and jane[0]["name"] == "brochure.pdf"
    assert bob and bob[0]["name"] == "brochure.pdf"


def test_use_as_attachment_applies_to_every_to(tmp_path, monkeypatch):
    from core import pdf_library as lib
    from core.style_draft import parse_directives

    monkeypatch.setattr(lib, "_LIB_DIR", tmp_path)
    lib.save_uploads([_Up("123_implementat.pdf", b"%PDF-123%")])
    msg = (
        "draft an email to jane@acme.com, bob@y.com "
        "like sent to jane@acme.com and use 123_implementat.pdf as attachment"
    )
    d = parse_directives(msg)
    assert [e.lower() for e in d["to_list"]] == ["jane@acme.com", "bob@y.com"]
    resolved = lib.resolve_message_attachments(
        msg,
        named=d["attachments"],
        named_by_email=d["attachments_by_email"],
        wants_attach=True,
    )
    for addr in ("jane@acme.com", "bob@y.com"):
        picked = lib.pick_for_recipient(
            addr,
            default=resolved["default"],
            by_email=resolved["by_email"],
        )
        assert picked and picked[0]["name"] == "123_implementat.pdf"


def test_mention_filename_selects_library_pdf(tmp_path, monkeypatch):
    from core import pdf_library as lib

    monkeypatch.setattr(lib, "_LIB_DIR", tmp_path)
    lib.save_uploads([_Up("one-pager.pdf", b"%PDF-1%")])
    resolved = lib.resolve_message_attachments(
        "draft to jane@acme.com using one-pager.pdf",
        wants_attach=False,
    )
    names = [a["name"] for a in (resolved["default"] or [])]
    assert names == ["one-pager.pdf"]


def test_save_non_pdf_and_match_stem(tmp_path, monkeypatch):
    from core import pdf_library as lib

    monkeypatch.setattr(lib, "_LIB_DIR", tmp_path)
    saved = lib.save_uploads(
        [_Up("rate-card.xlsx", b"PK\x03\x04fake", mime="application/vnd.ms-excel")]
    )
    assert saved and saved[0]["name"] == "rate-card.xlsx"
    rows = lib.list_files()
    hits = lib.match_query("rate-card", rows)
    assert hits and hits[0]["name"] == "rate-card.xlsx"
    loaded = lib.load_attachment(rows[0])
    assert loaded and loaded["name"] == "rate-card.xlsx"
    assert loaded["data"].startswith(b"PK")
