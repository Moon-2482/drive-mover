"""Google Drive adapters.

Two backends share one interface so the engine never cares which is live:

1. CliAdapter (default in this workspace) — shells out to the already-connected
   `hatch_gws_cli drive ...` connector. This reuses the user's existing
   authorised destination Drive. No token/secret ever appears in code, logs or
   frontend traffic; the CLI owns credentials.
2. DirectAdapter — raw Drive API v3 over HTTPS with OAuth 2.0 tokens that live
   ONLY in the server-side SQLite `tokens` table (see store.py). Used when the
   app is deployed standalone with its own Google Cloud OAuth client.
   Implements resumable uploads (8 MiB chunks, 308 resume) for large files.

Common interface:
  get_about() -> {user:{displayName,emailAddress}, storageQuota:{...}}
  get_file(id) -> metadata dict
  list_children(folder_id) -> [metadata] (all pages)
  list_folders(parent_id) -> [folder metadata] (for destination picker)
  create_folder(name, parent_id) -> metadata
  copy_file(source_id, name, parent_id) -> metadata
  download_file(source_id, dest_path) -> bytes written (streamed to disk)
  export_file(source_id, export_mime, dest_path) -> bytes written
  upload_file(local_path, name, parent_id, mime=None, convert_to=None) -> metadata
  trash_file(id) / update_metadata(id, fields)

Error type: DriveError(status, message, transient_bool).
Transient = 429 / 5xx / timeout / connection reset -> retry with backoff.
403/404 are NOT transient (permission / not found -> honest failure).
"""
import json
import os
import random
import subprocess
import time
import urllib.parse
import urllib.request
import urllib.error

FIELDS = ("id,name,mimeType,size,md5Checksum,parents,modifiedTime,createdTime,"
          "trashed,shortcutDetails,capabilities,copyRequiresWriterPermission,"
          "driveId,owners,webViewLink,version")
LIST_FIELDS = f"nextPageToken,files({FIELDS})"


class DriveError(Exception):
    def __init__(self, status, message, transient=False):
        super().__init__(message)
        self.status = status
        self.message = message
        self.transient = transient


def _classify(status, message):
    msg = (message or "").lower()
    if status in (429, 500, 502, 503, 504) or "rate limit" in msg \
            or "user rate limit" in msg or "backend error" in msg \
            or "internal error" in msg or "timed out" in msg:
        return True
    return False


def retry(fn, attempts=4, base=0.6):
    """Retry transient Drive errors with exponential backoff + jitter."""
    last = None
    for i in range(attempts):
        try:
            return fn()
        except DriveError as e:
            last = e
            if not e.transient or i == attempts - 1:
                raise
            time.sleep(base * (2 ** i) + random.uniform(0, 0.3))
        except (TimeoutError, ConnectionError, OSError) as e:
            last = DriveError(0, str(e), transient=True)
            if i == attempts - 1:
                raise last
            time.sleep(base * (2 ** i) + random.uniform(0, 0.3))
    raise last


# ---------------------------------------------------------------- CLI adapter
class CliAdapter:
    def __init__(self, cli="hatch_gws_cli", timeout=600):
        self.cli = cli
        self.timeout = timeout

    def _run(self, args, output_path=None):
        cmd = [self.cli, "drive"] + args
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise DriveError(504, "Drive CLI timed out", transient=True)
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        if p.returncode != 0:
            status = 0
            # CLI prints API errors like "Error: 403 ..." / "Error: ... Forbidden"
            for code in (403, 404, 429, 400, 500, 502, 503):
                if str(code) in err or str(code) in out:
                    status = code
                    break
            msg = err or out or f"Drive CLI failed (exit {p.returncode})"
            raise DriveError(status, msg, transient=_classify(status, msg))
        if output_path:
            return {"output_path": output_path}
        if not out:
            return {}
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            # Some commands print NDJSON (one object per line)
            objs = []
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("{"):
                    try:
                        objs.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
            if objs:
                return objs[0] if len(objs) == 1 else {"_pages": objs}
            return {"_raw": out}
        if isinstance(data, dict):
            data.pop("retrieved_at", None)
        return data

    def get_about(self):
        return retry(lambda: self._run([
            "about", "get", "--params",
            json.dumps({"fields": "user,storageQuota,maxUploadSize,maxImportSizes"})]))

    def get_file(self, fid):
        return retry(lambda: self._run([
            "files", "get", "--params",
            json.dumps({"fileId": fid, "fields": FIELDS, "supportsAllDrives": True})]))

    def list_children(self, folder_id):
        out = []
        token = None
        while True:
            params = {"q": f"'{folder_id}' in parents and trashed=false",
                      "pageSize": 1000, "fields": LIST_FIELDS,
                      "supportsAllDrives": True, "includeItemsFromAllDrives": True,
                      "corpora": "allDrives"}
            if token:
                params["pageToken"] = token
            data = retry(lambda p=params: self._run(["files", "list", "--params", json.dumps(p)]))
            out.extend(data.get("files", []) or [])
            token = data.get("nextPageToken")
            if not token:
                return out

    def list_folders(self, parent_id):
        q = (f"'{parent_id}' in parents and trashed=false and "
             "mimeType='application/vnd.google-apps.folder'")
        data = retry(lambda: self._run(["files", "list", "--params", json.dumps({
            "q": q, "pageSize": 200, "fields": LIST_FIELDS,
            "supportsAllDrives": True, "includeItemsFromAllDrives": True,
            "corpora": "allDrives", "orderBy": "name"})]))
        return data.get("files", []) or []

    def create_folder(self, name, parent_id):
        return retry(lambda: self._run([
            "files", "create", "--params",
            json.dumps({"supportsAllDrives": True, "ignoreDefaultVisibility": True}),
            "--json", json.dumps({"name": name,
                                   "mimeType": "application/vnd.google-apps.folder",
                                   "parents": [parent_id]})]))

    def copy_file(self, source_id, name, parent_id):
        return retry(lambda: self._run([
            "files", "copy", "--params",
            json.dumps({"fileId": source_id, "supportsAllDrives": True,
                        "ignoreDefaultVisibility": True}),
            "--json", json.dumps({"name": name, "parents": [parent_id]})]))

    def download_file(self, source_id, dest_path):
        retry(lambda: self._run([
            "files", "get", "--params",
            json.dumps({"fileId": source_id, "alt": "media", "supportsAllDrives": True}),
            "--output", dest_path], output_path=dest_path))
        return os.path.getsize(dest_path) if os.path.exists(dest_path) else 0

    def export_file(self, source_id, export_mime, dest_path):
        retry(lambda: self._run([
            "files", "export", "--params",
            json.dumps({"fileId": source_id, "mimeType": export_mime}),
            "--output", dest_path], output_path=dest_path))
        return os.path.getsize(dest_path) if os.path.exists(dest_path) else 0

    def upload_file(self, local_path, name, parent_id, mime=None, convert_to=None):
        args = ["+upload", local_path, "--parent", parent_id, "--name", name]
        data = retry(lambda: self._run(args))
        # +upload returns the created file metadata
        return data if isinstance(data, dict) else {}

    def trash_file(self, fid):
        return retry(lambda: self._run([
            "files", "update", "--params",
            json.dumps({"fileId": fid, "supportsAllDrives": True}),
            "--json", json.dumps({"trashed": True})]))

    def update_metadata(self, fid, fields):
        return retry(lambda: self._run([
            "files", "update", "--params",
            json.dumps({"fileId": fid, "supportsAllDrives": True}),
            "--json", json.dumps(fields)]))


# ------------------------------------------------------------- Direct API adapter
class DirectAdapter:
    """Drive API v3 via HTTPS. Token handling lives in the caller (server.py):
    it passes a token_provider() returning a fresh access token."""

    API = "https://www.googleapis.com/drive/v3"
    UPLOAD = "https://www.googleapis.com/upload/drive/v3"

    def __init__(self, token_provider):
        self.token_provider = token_provider

    def _req(self, method, url, body=None, headers=None, raw=False, timeout=600):
        token = self.token_provider()
        hdrs = {"Authorization": f"Bearer {token}"}
        if headers:
            hdrs.update(headers)
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if raw:
                    return r.read()
                payload = r.read()
                return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode() or "{}")
                msg = detail.get("error", {}).get("message", str(e))
            except Exception:
                msg = str(e)
            raise DriveError(e.code, msg, transient=_classify(e.code, msg))

    def get_about(self):
        return retry(lambda: self._req(
            "GET", f"{self.API}/about?fields=user,storageQuota,maxUploadSize,maxImportSizes"))

    def get_file(self, fid):
        q = urllib.parse.urlencode({"fields": FIELDS, "supportsAllDrives": "true"})
        return retry(lambda: self._req("GET", f"{self.API}/files/{fid}?{q}"))

    def list_children(self, folder_id):
        out, token = [], None
        while True:
            q = urllib.parse.urlencode({
                "q": f"'{folder_id}' in parents and trashed=false",
                "pageSize": 1000, "fields": LIST_FIELDS,
                "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
                "corpora": "allDrives",
                **({"pageToken": token} if token else {})})
            data = retry(lambda: self._req("GET", f"{self.API}/files?{q}"))
            out.extend(data.get("files", []) or [])
            token = data.get("nextPageToken")
            if not token:
                return out

    def list_folders(self, parent_id):
        q = urllib.parse.urlencode({
            "q": f"'{parent_id}' in parents and trashed=false and "
                 "mimeType='application/vnd.google-apps.folder'",
            "pageSize": 200, "fields": LIST_FIELDS,
            "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
            "corpora": "allDrives", "orderBy": "name"})
        data = retry(lambda: self._req("GET", f"{self.API}/files?{q}"))
        return data.get("files", []) or []

    def create_folder(self, name, parent_id):
        return retry(lambda: self._req("POST", f"{self.API}/files?supportsAllDrives=true", {
            "name": name, "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id]}))

    def copy_file(self, source_id, name, parent_id):
        return retry(lambda: self._req(
            "POST", f"{self.API}/files/{source_id}/copy?supportsAllDrives=true",
            {"name": name, "parents": [parent_id]}))

    def download_file(self, source_id, dest_path):
        token = self.token_provider()
        req = urllib.request.Request(
            f"{self.API}/files/{source_id}?alt=media&supportsAllDrives=true",
            headers={"Authorization": f"Bearer {token}"}, method="GET")
        total = 0
        try:
            with urllib.request.urlopen(req, timeout=600) as r, open(dest_path, "wb") as f:
                while True:
                    chunk = r.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    total += len(chunk)
        except urllib.error.HTTPError as e:
            raise DriveError(e.code, f"download failed: {e}", transient=_classify(e.code, str(e)))
        return total

    def export_file(self, source_id, export_mime, dest_path):
        token = self.token_provider()
        q = urllib.parse.urlencode({"mimeType": export_mime})
        req = urllib.request.Request(
            f"{self.API}/files/{source_id}/export?{q}",
            headers={"Authorization": f"Bearer {token}"}, method="GET")
        total = 0
        try:
            with urllib.request.urlopen(req, timeout=600) as r, open(dest_path, "wb") as f:
                while True:
                    chunk = r.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    total += len(chunk)
        except urllib.error.HTTPError as e:
            raise DriveError(e.code, f"export failed: {e}", transient=_classify(e.code, str(e)))
        return total

    def upload_file(self, local_path, name, parent_id, mime=None, convert_to=None):
        """Resumable upload: session -> PUT chunks (8 MiB) -> handles 308 resume."""
        import mimetypes
        mime = mime or mimetypes.guess_type(local_path)[0] or "application/octet-stream"
        meta = {"name": name, "parents": [parent_id]}
        if convert_to:
            meta["mimeType"] = convert_to
        token = self.token_provider()
        size = os.path.getsize(local_path)
        req = urllib.request.Request(
            f"{self.UPLOAD}/files?uploadType=resumable&supportsAllDrives=true",
            data=json.dumps(meta).encode(),
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json; charset=UTF-8",
                     "X-Upload-Content-Type": mime,
                     "X-Upload-Content-Length": str(size)}, method="POST")
        with urllib.request.urlopen(req, timeout=120) as r:
            session = r.headers.get("Location")
        if not session:
            raise DriveError(500, "Resumable upload session was not created", transient=True)
        chunk = 8 * 1024 * 1024
        offset = 0
        with open(local_path, "rb") as f:
            while offset < size:
                data = f.read(chunk)
                end = offset + len(data) - 1
                put = urllib.request.Request(
                    session, data=data, method="PUT",
                    headers={"Content-Type": mime,
                             "Content-Length": str(len(data)),
                             "Content-Range": f"bytes {offset}-{end}/{size}"})
                try:
                    with urllib.request.urlopen(put, timeout=600) as r:
                        if r.status in (200, 201):
                            return json.loads(r.read() or b"{}")
                except urllib.error.HTTPError as e:
                    if e.code == 308:
                        rng = e.headers.get("Range", "")
                        if rng:
                            offset = int(rng.split("-")[1]) + 1
                            f.seek(offset)
                            continue
                        offset = end + 1
                        continue
                    raise DriveError(e.code, f"upload failed: {e}",
                                     transient=_classify(e.code, str(e)))
                offset = end + 1
        raise DriveError(500, "Upload ended without a server response", transient=True)

    def trash_file(self, fid):
        return retry(lambda: self._req(
            "PATCH", f"{self.API}/files/{fid}?supportsAllDrives=true", {"trashed": True}))

    def update_metadata(self, fid, fields):
        return retry(lambda: self._req(
            "PATCH", f"{self.API}/files/{fid}?supportsAllDrives=true", fields))
