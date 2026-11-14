"""Parse Google Drive folder / file links into resource IDs.

Supported forms (all handled without any network call):
  https://drive.google.com/drive/folders/<ID>
  https://drive.google.com/drive/u/0/folders/<ID>?usp=sharing
  https://drive.google.com/drive/folders/<ID>?usp=drive_link
  https://drive.google.com/open?id=<ID>
  https://drive.google.com/file/d/<ID>/view
  https://docs.google.com/document/d/<ID>/...
  bare ID (26+ chars of [A-Za-z0-9_-])
  https://drive.google.com/drive/shared-with-me  -> error (no ID)
"""
import re
from urllib.parse import urlparse, parse_qs

_ID = r"([A-Za-z0-9_-]{10,})"

_PATTERNS = [
    re.compile(r"/drive/folders/" + _ID),
    re.compile(r"/drive/u/\d+/folders/" + _ID),
    re.compile(r"/file/d/" + _ID),
    re.compile(r"/document/d/" + _ID),
    re.compile(r"/spreadsheets/d/" + _ID),
    re.compile(r"/presentation/d/" + _ID),
    re.compile(r"/drawing/d/" + _ID),
    re.compile(r"/forms/d/" + _ID),
    re.compile(r"[?&]id=" + _ID),
    re.compile(r"[?&]folders/" + _ID),
]


class LinkParseError(ValueError):
    pass


def parse_drive_link(link: str) -> dict:
    """Return {'id': ..., 'kind_hint': 'folder'|'file'|'unknown'}."""
    if not link or not str(link).strip():
        raise LinkParseError("Empty link. Paste a Google Drive folder link.")
    s = str(link).strip()
    # Bare ID
    if re.fullmatch(r"[A-Za-z0-9_-]{25,}", s):
        return {"id": s, "kind_hint": "unknown"}
    if "drive.google.com" not in s and "docs.google.com" not in s:
        raise LinkParseError(
            "That does not look like a Google Drive link. "
            "Paste a link like https://drive.google.com/drive/folders/<id>."
        )
    kind_hint = "folder"
    if "/file/d/" in s or "/document/d/" in s or "/spreadsheets/d/" in s \
            or "/presentation/d/" in s or "/drawing/d/" in s or "/forms/d/" in s:
        kind_hint = "file"
    for pat in _PATTERNS:
        m = pat.search(s)
        if m:
            return {"id": m.group(1), "kind_hint": kind_hint}
    # open?id= via proper query parse as a fallback
    try:
        qs = parse_qs(urlparse(s).query)
        if qs.get("id"):
            return {"id": qs["id"][0], "kind_hint": kind_hint}
    except Exception:
        pass
    raise LinkParseError(
        "Could not find a file/folder ID in that link. "
        "Open the folder in Drive, copy the link from the address bar, and paste it here."
    )


def is_folder_mime(mime: str) -> bool:
    return mime == "application/vnd.google-apps.folder"


def is_shortcut_mime(mime: str) -> bool:
    return mime == "application/vnd.google-apps.shortcut"


# Google Workspace native types -> export MIME for the fallback path
# (used ONLY when server-side files.copy is refused by Drive).
NATIVE_EXPORT = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
    "application/vnd.google-apps.drawing": ("image/png", ".png"),
    # Apps Script / Forms / Maps / Sites / Jamboard have no supported
    # Drive export -> reported as unsupported, never silently skipped.
}

GOOGLE_NATIVE_MIMES = set(NATIVE_EXPORT) | {
    "application/vnd.google-apps.form",
    "application/vnd.google-apps.script",
    "application/vnd.google-apps.map",
    "application/vnd.google-apps.site",
    "application/vnd.google-apps.jam",
    "application/vnd.google-apps.video",
    "application/vnd.google-apps.audio",
    "application/vnd.google-apps.photo",
}
