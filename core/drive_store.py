# NOTE: Persist large Relay blobs on Google Drive (drive.file scope) to cut Render RAM.
from __future__ import annotations

import io
import json
import sys
import threading
from typing import Any, Optional

from core.google_sheets import sheets_credentials

FOLDER_NAME = "Relay Memory"
_FILE_IDS: dict[str, str] = {}
_FOLDER_ID: Optional[str] = None
_LOCK = threading.Lock()
_DRIVE = None


def _drive():
    global _DRIVE
    if _DRIVE is not None:
        return _DRIVE
    creds = sheets_credentials()
    if not creds:
        return None
    # Ensure drive.file is available on the token
    scopes = set(getattr(creds, "scopes", None) or [])
    if scopes and "https://www.googleapis.com/auth/drive.file" not in scopes:
        print("[drive] token missing drive.file scope", file=sys.stderr)
        return None
    try:
        from googleapiclient.discovery import build

        _DRIVE = build("drive", "v3", credentials=creds, cache_discovery=False)
        return _DRIVE
    except Exception as e:
        print(f"[drive] build failed: {e}", file=sys.stderr)
        return None


def _ensure_folder() -> Optional[str]:
    global _FOLDER_ID
    if _FOLDER_ID:
        return _FOLDER_ID
    svc = _drive()
    if not svc:
        return None
    try:
        q = (
            "mimeType='application/vnd.google-apps.folder' "
            f"and name='{FOLDER_NAME}' and trashed=false"
        )
        res = (
            svc.files()
            .list(q=q, spaces="drive", fields="files(id,name)", pageSize=5)
            .execute()
        )
        files = res.get("files") or []
        if files:
            _FOLDER_ID = files[0]["id"]
            return _FOLDER_ID
        meta = (
            svc.files()
            .create(
                body={
                    "name": FOLDER_NAME,
                    "mimeType": "application/vnd.google-apps.folder",
                },
                fields="id",
            )
            .execute()
        )
        _FOLDER_ID = meta["id"]
        return _FOLDER_ID
    except Exception as e:
        print(f"[drive] folder failed: {e}", file=sys.stderr)
        return None


def _find_file(name: str, folder_id: str) -> Optional[str]:
    if name in _FILE_IDS:
        return _FILE_IDS[name]
    svc = _drive()
    if not svc:
        return None
    try:
        q = (
            f"name='{name}' and '{folder_id}' in parents and trashed=false"
        )
        res = (
            svc.files()
            .list(q=q, spaces="drive", fields="files(id,name)", pageSize=5)
            .execute()
        )
        files = res.get("files") or []
        if files:
            _FILE_IDS[name] = files[0]["id"]
            return files[0]["id"]
    except Exception as e:
        print(f"[drive] find {name} failed: {e}", file=sys.stderr)
    return None


def upload_json(name: str, payload: Any) -> bool:
    """Create or update a JSON file in the Relay Memory Drive folder."""
    from googleapiclient.http import MediaIoBaseUpload

    folder = _ensure_folder()
    svc = _drive()
    if not folder or not svc:
        return False
    try:
        raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        # Cap ~4MB to avoid blowing free-tier RAM while buffering
        if len(raw) > 4_000_000:
            if isinstance(payload, list):
                payload = payload[-2000:]
                raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            else:
                print(f"[drive] skip oversized {name}", file=sys.stderr)
                return False
        media = MediaIoBaseUpload(
            io.BytesIO(raw), mimetype="application/json", resumable=False
        )
        with _LOCK:
            file_id = _find_file(name, folder)
            if file_id:
                svc.files().update(fileId=file_id, media_body=media).execute()
            else:
                meta = (
                    svc.files()
                    .create(
                        body={"name": name, "parents": [folder]},
                        media_body=media,
                        fields="id",
                    )
                    .execute()
                )
                _FILE_IDS[name] = meta["id"]
        return True
    except Exception as e:
        print(f"[drive] upload {name} failed: {e}", file=sys.stderr)
        return False


def download_json(name: str) -> Optional[Any]:
    folder = _ensure_folder()
    svc = _drive()
    if not folder or not svc:
        return None
    try:
        file_id = _find_file(name, folder)
        if not file_id:
            return None
        raw = svc.files().get_media(fileId=file_id).execute()
        if isinstance(raw, bytes):
            text = raw.decode("utf-8")
        else:
            text = str(raw)
        return json.loads(text)
    except Exception as e:
        print(f"[drive] download {name} failed: {e}", file=sys.stderr)
        return None


def upload_json_async(name: str, payload: Any) -> None:
    def _run() -> None:
        upload_json(name, payload)

    threading.Thread(target=_run, daemon=True, name=f"drive-{name}").start()
