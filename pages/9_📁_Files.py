# NOTE: File library — upload once, attach from Chat by filename.
from __future__ import annotations

from datetime import datetime

import streamlit as st

from config import APP_NAME
from core.auth_ui import logout_button, require_login
from core.pdf_library import (
    delete_file,
    file_icon,
    lib_dir,
    list_files,
    save_uploads,
)

st.set_page_config(page_title=f"Files · {APP_NAME}", page_icon="📁", layout="wide")
if not require_login():
    st.stop()
logout_button()

st.title("📁 Files")
st.caption(
    "Upload decks, one-pagers, and other attachments here. "
    "In **💬 Chat**, name the file to attach it to a draft — "
    "`attach one-pager.pdf`, or `to jane@x.com attach brochure.pdf`."
)

if "file_lib_uploader_n" not in st.session_state:
    st.session_state.file_lib_uploader_n = 0

uploads = st.file_uploader(
    "Upload files",
    accept_multiple_files=True,
    key=f"file_lib_upload_{st.session_state.file_lib_uploader_n}",
    help="PDF, Word, Excel, PowerPoint, images, zip. Keep each file under 25 MB for Gmail.",
)
if uploads:
    saved = save_uploads(list(uploads))
    skipped = len(uploads) - len(saved)
    st.session_state.file_lib_uploader_n = int(st.session_state.file_lib_uploader_n) + 1
    if saved:
        st.toast("Saved " + ", ".join(r.get("name") or "file" for r in saved))
    if skipped:
        st.warning(f"{skipped} file(s) skipped (over 25 MB or unreadable).")
    st.rerun()

all_rows = list_files()
rows = list(reversed(all_rows))
q = st.text_input("Search files", placeholder="one-pager, brochure, deck…")
if (q or "").strip():
    needle = q.strip().lower()
    rows = [r for r in rows if needle in str(r.get("name") or "").lower()]

m1, m2, m3 = st.columns(3)
m1.metric("Files", len(all_rows))
m2.metric("Showing", len(rows))
total_bytes = sum(int(r.get("size") or 0) for r in all_rows)
if total_bytes < 1024 * 1024:
    m3.metric("Library size", f"{max(total_bytes / 1024, 0):.1f} KB")
else:
    m3.metric("Library size", f"{total_bytes / (1024 * 1024):.1f} MB")

if not rows:
    st.info(
        "No files yet. Upload above, then in Chat say "
        "`draft to name@org.com attach one-pager.pdf`."
    )
    st.stop()

h1, h2, h3, h4 = st.columns([4, 1, 2, 2])
h1.markdown("**Name**")
h2.markdown("**Size**")
h3.markdown("**Added**")
h4.markdown("**Actions**")

for row in rows:
    pid = str(row.get("id") or "")
    name = str(row.get("name") or "file")
    size_label = str(row.get("size_label") or "")
    added = str(row.get("added_at") or "")
    added_short = added[:10] if added else "—"
    try:
        if added.endswith("Z"):
            added_short = datetime.fromisoformat(added.replace("Z", "+00:00")).strftime(
                "%Y-%m-%d"
            )
    except Exception:
        pass
    c1, c2, c3, c4 = st.columns([4, 1, 2, 2])
    c1.markdown(f"{file_icon(name)} `{name}`")
    c2.caption(size_label)
    c3.caption(added_short)
    with c4:
        d1, d2 = st.columns(2)
        stored = lib_dir() / str(row.get("stored_as") or "")
        if stored.is_file():
            d1.download_button(
                "⬇",
                data=stored.read_bytes(),
                file_name=name,
                mime=str(row.get("mime") or "application/octet-stream"),
                key=f"file_dl_{pid}",
                help=f"Download {name}",
                use_container_width=True,
            )
        if d2.button("Delete", key=f"file_del_{pid}", use_container_width=True):
            delete_file(pid)
            st.rerun()
