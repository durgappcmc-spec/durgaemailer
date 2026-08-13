# NOTE: Persist large Relay blobs on Google Drive (drive.file scope) to cut Render RAM.
from __future__ import annotations

import io
import json
import os
import sys
import threading
from typing import Any, Optional

from core.google_sheets import sheets_credentials

FOLDER_NAME = "Relay Memory"
_FILE_IDS: dict[str, str] = {}
_FOLDER_ID: Optional[str] = None
_LOCK = threading.Lock()
_DRIVE = None


def clear_file_cache() -> None:
    """Drop cached Drive file ids (retry after a failed update)."""
    global _FILE_IDS
    with _LOCK:
        _FILE_IDS = {}


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


def _pinned_folder_id() -> str:
    try:
        from config import settings

        return str(getattr(settings, "RELAY_DRIVE_FOLDER_ID", "") or "").strip()
    except Exception:
        return (os.getenv("RELAY_DRIVE_FOLDER_ID") or "").strip()


def _probe_file_id(name: str, folder_id: str) -> Optional[str]:
    """Look up a file id in a folder without touching the process cache."""
    svc = _drive()
    if not svc or not folder_id:
        return None
    try:
        q = f"name='{name}' and '{folder_id}' in parents and trashed=false"
        res = (
            svc.files()
            .list(q=q, spaces="drive", fields="files(id,name,size)", pageSize=5)
            .execute()
        )
        files = res.get("files") or []
        if files:
            return files[0]["id"]
    except Exception as e:
        print(f"[drive] probe {name} failed: {e}", file=sys.stderr)
    return None


def _probe_file_size(name: str, folder_id: str) -> int:
    svc = _drive()
    if not svc or not folder_id:
        return 0
    try:
        q = f"name='{name}' and '{folder_id}' in parents and trashed=false"
        res = (
            svc.files()
            .list(q=q, spaces="drive", fields="files(id,size)", pageSize=5)
            .execute()
        )
        files = res.get("files") or []
        if not files:
            return 0
        return int(files[0].get("size") or 0)
    except Exception:
        return 0


def _pick_best_memory_folder(folders: list[dict[str, Any]]) -> Optional[str]:
    """Prefer the Relay Memory folder that already holds prospect/chat blobs.

    Without RELAY_DRIVE_FOLDER_ID, drive.file can leave multiple 'Relay Memory'
    folders after redeploys. Picking files[0] often attaches to an empty one.
    """
    if not folders:
        return None
    if len(folders) == 1:
        return folders[0]["id"]
    scored: list[tuple[int, str]] = []
    for f in folders:
        fid = str(f.get("id") or "")
        if not fid:
            continue
        # Weight durable blobs so we land on the folder with real history
        score = 0
        for blob, weight in (
            ("relay_prospects.json", 1000),
            ("relay_memory.json", 100),
            ("relay_chat.json", 50),
            ("relay_session.json", 10),
            ("relay_aliases.json", 5),
        ):
            sz = _probe_file_size(blob, fid)
            if sz > 0:
                score += weight + min(sz, 500_000) // 1000
        scored.append((score, fid))
        print(f"[drive] candidate folder {fid} score={score}", file=sys.stderr)
    scored.sort(key=lambda t: t[0], reverse=True)
    best_score, best_id = scored[0]
    if best_score == 0:
        # All empty — keep first for stability, but scream for a pin
        print(
            "[drive] multiple empty Relay Memory folders; "
            "set RELAY_DRIVE_FOLDER_ID to the one with your data",
            file=sys.stderr,
        )
        return folders[0]["id"]
    if len(scored) > 1 and scored[1][0] > 0:
        print(
            f"[drive] chose folder {best_id} (score={best_score}) among "
            f"{len(folders)} Relay Memory folders — pin RELAY_DRIVE_FOLDER_ID",
            file=sys.stderr,
        )
    return best_id


def _ensure_folder() -> Optional[str]:
    global _FOLDER_ID
    if _FOLDER_ID:
        return _FOLDER_ID
    pinned = _pinned_folder_id()
    if pinned:
        _FOLDER_ID = pinned
        print(f"[drive] using pinned folder id={pinned}", file=sys.stderr)
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
            .list(q=q, spaces="drive", fields="files(id,name)", pageSize=20)
            .execute()
        )
        files = res.get("files") or []
        if files:
            chosen = _pick_best_memory_folder(files)
            if chosen:
                _FOLDER_ID = chosen
                print(
                    f"[drive] using folder '{FOLDER_NAME}' id={_FOLDER_ID} "
                    f"(set RELAY_DRIVE_FOLDER_ID on Render to pin across deploys)",
                    file=sys.stderr,
                )
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
        print(
            f"[drive] created folder '{FOLDER_NAME}' id={_FOLDER_ID} "
            f"(set RELAY_DRIVE_FOLDER_ID to pin across deploys)",
            file=sys.stderr,
        )
        return _FOLDER_ID
    except Exception as e:
        print(f"[drive] folder failed: {e}", file=sys.stderr)
        return None


def memory_status() -> dict[str, Any]:
    """Diagnostics for Prospects / Memory UI after redeploys."""
    pinned = _pinned_folder_id()
    folder = _ensure_folder()
    prospects_n = 0
    has_prospects_file = False
    drive_ok = _drive() is not None
    if folder and drive_ok:
        try:
            data = download_json("relay_prospects.json")
            if isinstance(data, list):
                has_prospects_file = True
                prospects_n = len([r for r in data if isinstance(r, dict)])
            elif data is not None:
                has_prospects_file = True
        except Exception as e:
            print(f"[drive] memory_status: {e}", file=sys.stderr)
    return {
        "drive_ok": drive_ok,
        "folder_id": folder or "",
        "folder_pinned": bool(pinned),
        "pinned_folder_id": pinned,
        "has_prospects_file": has_prospects_file,
        "prospects_count": prospects_n,
    }


def _find_file(name: str, folder_id: str) -> Optional[str]:
    if name in _FILE_IDS:
        return _FILE_IDS[name]
    file_id = _probe_file_id(name, folder_id)
    if file_id:
        _FILE_IDS[name] = file_id
        return file_id
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
