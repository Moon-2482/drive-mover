"""HTTP server: dashboard + JSON API + OAuth (api mode).

Run:  python -m drive_migration.server   (or ./run.sh)
Env:
  PORT=8765  HOST=127.0.0.1
  DRIVEMIG_MODE=cli|api        (default cli — reuses the connected Drive)
  DRIVEMIG_DB=./data/migrations.db
  DRIVEMIG_TMPDIR=./data/tmp
  GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REDIRECT_URI  (api mode)
  GOOGLE_OAUTH_SCOPES (default: drive scope below)

OAuth scopes requested (least privilege that still allows migration):
  https://www.googleapis.com/auth/drive
  Rationale: files.copy / export / upload into the user's own Drive, plus
  reading shared folders the account can already access, cannot be done with
  drive.file alone (drive.file only sees files the app itself created).
  drive.readonly alone cannot copy. No gmail/calendar/etc scopes.

Tokens live ONLY in the server-side SQLite `tokens` table. They are never
sent to the browser, never logged, never put in URLs after the callback.
"""
import base64
import csv
import hashlib
import io
import json
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .store import Store
from .linkparse import parse_drive_link, LinkParseError
from .drive_adapter import CliAdapter, DirectAdapter, DriveError
from .engine import Engine

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND = os.path.join(BASE, "frontend")

MODE = os.environ.get("DRIVEMIG_MODE", "cli").lower()
PORT = int(os.environ.get("PORT", "8765"))
HOST = os.environ.get("HOST", "127.0.0.1")
DB_PATH = os.environ.get("DRIVEMIG_DB", os.path.join(BASE, "data", "migrations.db"))
TMPDIR = os.environ.get("DRIVEMIG_TMPDIR", os.path.join(BASE, "data", "tmp"))
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
os.makedirs(TMPDIR, exist_ok=True)

SCOPES = os.environ.get(
    "GOOGLE_OAUTH_SCOPES",
    "https://www.googleapis.com/auth/drive").split()

store = Store(DB_PATH)
_oauth_state = {}  # state -> {code_verifier, created_at}
_workers = {}
_workers_guard = threading.Lock()


# ------------------------------------------------------------------ adapter
def _refresh_access_token():
    tok = store.get_token()
    if not tok or not tok.get("refresh_token"):
        raise DriveError(401, "Not connected. Connect Google Drive first.", transient=False)
    if tok.get("access_token") and tok.get("expires_at", 0) > time.time() + 60:
        return tok["access_token"]
    data = urllib.parse.urlencode({
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
        "refresh_token": tok["refresh_token"],
        "grant_type": "refresh_token"}).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.loads(r.read())
    store.save_token(resp["access_token"], tok.get("refresh_token"),
                     time.time() + int(resp.get("expires_in", 3600)), " ".join(SCOPES))
    return resp["access_token"]


def get_adapter():
    if MODE == "api":
        return DirectAdapter(_refresh_access_token)
    return CliAdapter()


def get_engine():
    return Engine(store, get_adapter(), tmpdir=TMPDIR)


def _start_worker(job_id, fn):
    with _workers_guard:
        t = _workers.get(job_id)
        if t and t.is_alive():
            return
        t = threading.Thread(target=fn, args=(job_id,), daemon=True)
        _workers[job_id] = t
        t.start()


def _worker_scan(job_id):
    try:
        get_engine().scan(job_id)
    except Exception as e:
        j = store.get_job(job_id)
        if j:
            store.update_job(job_id, status="failed", scan_error=str(e))
            store.log(job_id, "error", f"Scan crashed: {e}")


def _worker_run(job_id):
    try:
        get_engine().run_migration(job_id)
    except Exception as e:
        j = store.get_job(job_id)
        if j:
            store.log(job_id, "error", f"Worker crashed: {e}")


# ------------------------------------------------------------------ handler
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # keep stdout clean
        pass

    # -- helpers --
    def _send(self, code, obj=None, ctype="application/json", headers=None):
        body = b""
        if obj is not None:
            body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    def _route(self):
        p = urllib.parse.urlparse(self.path)
        return p.path, urllib.parse.parse_qs(p.query)

    # -- static --
    def _serve_file(self, name, ctype):
        path = os.path.join(FRONTEND, name)
        if not os.path.exists(path):
            self._send(404, {"error": "not found"})
            return
        with open(path, "rb") as f:
            self._send(200, f.read(), ctype=ctype)

    # -- API --
    def do_GET(self):
        path, qs = self._route()
        try:
            if path == "/" or path == "/index.html":
                return self._serve_file("index.html", "text/html; charset=utf-8")
            if path in ("/app.js", "/styles.css"):
                ct = "text/javascript" if path.endswith(".js") else "text/css"
                return self._serve_file(path[1:], ct)
            if path == "/api/status":
                return self._api_status()
            if path == "/api/dest-folders":
                return self._api_dest_folders(qs)
            if path == "/api/jobs":
                return self._send(200, {"jobs": store.list_jobs()})
            if path.startswith("/api/jobs/"):
                return self._api_job_get(path, qs)
            if path == "/auth/status":
                return self._send(200, {"mode": MODE, "connected": self._connected()})
            if path == "/auth/login":
                return self._auth_login()
            if path == "/auth/callback":
                return self._auth_callback(qs)
            return self._send(404, {"error": "unknown route"})
        except DriveError as e:
            self._send(e.status or 500, {"error": e.message})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def do_POST(self):
        path, qs = self._route()
        try:
            if path == "/api/jobs":
                return self._api_create_job()
            if path.startswith("/api/jobs/"):
                return self._api_job_action(path)
            if path == "/auth/logout":
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "unknown route"})
        except DriveError as e:
            self._send(e.status or 500, {"error": e.message})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def _connected(self):
        if MODE == "cli":
            try:
                get_adapter().get_about()
                return True
            except Exception:
                return False
        tok = store.get_token()
        return bool(tok and tok.get("refresh_token"))

    def _api_status(self):
        try:
            about = get_adapter().get_about()
            user = about.get("user", {})
            quota = about.get("storageQuota", {})
            return self._send(200, {
                "mode": MODE,
                "connected": True,
                "account": {"name": user.get("displayName", ""),
                            "email": user.get("emailAddress", "")},
                "quota": {"limit": quota.get("limit"), "usage": quota.get("usage"),
                          "usageInDrive": quota.get("usageInDrive")},
                "auth_url": None})
        except DriveError as e:
            return self._send(200, {
                "mode": MODE, "connected": False, "account": None,
                "quota": None,
                "auth_url": "/auth/login" if MODE == "api" else None,
                "error": e.message})

    def _api_dest_folders(self, qs):
        parent = (qs.get("parent") or ["root"])[0]
        try:
            folders = get_adapter().list_folders(parent)
        except DriveError as e:
            return self._send(e.status or 500, {"error": e.message})
        return self._send(200, {"parent": parent,
                                "folders": [{"id": f["id"], "name": f["name"]}
                                            for f in folders]})

    def _api_create_job(self):
        body = self._read_json()
        link = (body.get("sourceLink") or "").strip()
        try:
            parsed = parse_drive_link(link)
        except LinkParseError as e:
            return self._send(400, {"error": str(e)})
        dest_parent = (body.get("destParentId") or "root").strip() or "root"
        policy = body.get("duplicatePolicy") or "rename"
        if policy not in ("skip", "rename", "replace"):
            return self._send(400, {"error": "Invalid duplicate policy"})
        confirmed = bool(body.get("confirmedReplace"))
        if policy == "replace" and not confirmed:
            return self._send(400, {"error": "Replace policy needs explicit confirmation."})
        # Validate source is reachable + a folder, and dest exists — before
        # creating the job, so a typo'd link fails fast with a clear message.
        try:
            src = get_adapter().get_file(parsed["id"])
        except DriveError as e:
            return self._send(400, {
                "error": f"Cannot access that folder: {e.message} (HTTP {e.status}). "
                         "Check the link is shared with the connected account, or "
                         "that the folder has not been deleted / restricted."})
        if src.get("mimeType") != "application/vnd.google-apps.folder":
            return self._send(400, {"error": "That link points to a single file, not a folder."})
        dest_name = "My Drive"
        if dest_parent != "root":
            try:
                d = get_adapter().get_file(dest_parent)
                dest_name = d.get("name", dest_parent)
            except DriveError:
                dest_name = dest_parent
        jid = store.create_job(link, parsed["id"], dest_parent, dest_name,
                               policy, confirmed)
        store.log(jid, "info",
                  f"Job created: source '{src.get('name')}' -> destination '{dest_name}' "
                  f"(duplicate policy: {policy}).")
        return self._send(201, {"job_id": jid, "source_name": src.get("name")})

    def _api_job_get(self, path, qs):
        parts = [p for p in path.split("/") if p]
        # /api/jobs/<id>[/items|logs|manifest|report.csv]
        if len(parts) < 3:
            return self._send(404, {"error": "unknown route"})
        jid = parts[2]
        job = store.get_job(jid)
        if not job:
            return self._send(404, {"error": "job not found"})
        if len(parts) == 3:
            return self._send(200, {"job": job})
        sub = parts[3]
        if sub == "items":
            status = (qs.get("status") or [None])[0]
            limit = int((qs.get("limit") or ["500"])[0])
            offset = int((qs.get("offset") or ["0"])[0])
            return self._send(200, {"items": store.get_items(jid, status, limit, offset),
                                    "total": store.count_items(jid)})
        if sub == "logs":
            return self._send(200, {"logs": store.get_logs(jid)})
        if sub == "manifest":
            items = store.get_items(jid, None, limit=200000)
            return self._send(200, {"job": job, "items": items})
        if sub == "report.csv":
            items = store.get_items(jid, None, limit=200000)
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["path", "name", "kind", "mime", "size", "status",
                        "method", "source_id", "dest_id", "error"])
            for it in items:
                w.writerow([it.get("path"), it.get("name"), it.get("kind"),
                            it.get("mime"), it.get("size"), it.get("status"),
                            it.get("method"), it.get("source_id"), it.get("dest_id"),
                            it.get("error")])
            return self._send(200, buf.getvalue().encode(), ctype="text/csv",
                              headers={"Content-Disposition":
                                       f"attachment; filename=migration-{jid}.csv"})
        return self._send(404, {"error": "unknown route"})

    def _api_job_action(self, path):
        parts = [p for p in path.split("/") if p]
        if len(parts) != 4:
            return self._send(404, {"error": "unknown route"})
        jid, action = parts[2], parts[3]
        job = store.get_job(jid)
        if not job:
            return self._send(404, {"error": "job not found"})
        if action == "scan":
            if job["status"] not in ("created", "failed"):
                return self._send(409, {"error": f"Cannot scan while job is {job['status']}"})
            _start_worker(jid, _worker_scan)
            return self._send(200, {"ok": True, "status": "scanning"})
        if action == "start":
            if job["status"] not in ("scanned", "paused"):
                return self._send(409, {"error": f"Cannot start while job is {job['status']}. Scan first."})
            store.update_job(jid, status="running")
            _start_worker(jid, _worker_run)
            return self._send(200, {"ok": True, "status": "running"})
        if action == "pause":
            if job["status"] != "running":
                return self._send(409, {"error": "Only a running job can be paused."})
            store.update_job(jid, status="paused")
            store.log(jid, "info", "Pause requested — worker will stop after the current file.")
            return self._send(200, {"ok": True})
        if action == "resume":
            if job["status"] not in ("paused",):
                return self._send(409, {"error": f"Cannot resume while job is {job['status']}"})
            store.update_job(jid, status="running")
            _start_worker(jid, _worker_run)
            return self._send(200, {"ok": True})
        if action == "cancel":
            if job["status"] in ("completed", "cancelled"):
                return self._send(409, {"error": "Job already finished."})
            store.update_job(jid, status="cancelled", current_file="")
            store.log(jid, "info", "Migration cancelled by user.")
            return self._send(200, {"ok": True})
        if action == "retry-failed":
            if job["status"] not in ("completed", "failed", "paused", "scanned"):
                return self._send(409, {"error": f"Cannot retry while job is {job['status']}"})
            store.reset_failed(jid)
            store.refresh_counts(jid)
            store.update_job(jid, status="running")
            store.log(jid, "info", "Retrying failed / unsupported items.")
            _start_worker(jid, _worker_run)
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "unknown action"})

    # -- OAuth (api mode only) --
    def _auth_login(self):
        if MODE != "api":
            return self._send(400, {"error": "This deployment uses the connected Drive; no OAuth login needed."})
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        state = secrets.token_urlsafe(32)
        _oauth_state[state] = {"verifier": verifier, "at": time.time()}
        params = urllib.parse.urlencode({
            "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "redirect_uri": os.environ["GOOGLE_REDIRECT_URI"],
            "response_type": "code", "scope": " ".join(SCOPES),
            "access_type": "offline", "prompt": "consent",
            "state": state, "code_challenge": challenge,
            "code_challenge_method": "S256"})
        self.send_response(302)
        self.send_header("Location", f"https://accounts.google.com/o/oauth2/v2/auth?{params}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _auth_callback(self, qs):
        if MODE != "api":
            return self._send(400, {"error": "OAuth is not used in cli mode."})
        code = (qs.get("code") or [None])[0]
        state = (qs.get("state") or [None])[0]
        saved = _oauth_state.pop(state, None)
        if not code or not saved:
            return self._send(400, {"error": "Invalid OAuth callback."})
        data = urllib.parse.urlencode({
            "code": code, "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "redirect_uri": os.environ["GOOGLE_REDIRECT_URI"],
            "grant_type": "authorization_code",
            "code_verifier": saved["verifier"]}).encode()
        req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                resp = json.loads(r.read())
        except urllib.error.HTTPError as e:
            return self._send(400, {"error": f"Token exchange failed: {e}"})
        store.save_token(resp["access_token"], resp.get("refresh_token", ""),
                         time.time() + int(resp.get("expires_in", 3600)),
                         resp.get("scope", ""))
        self.send_response(302)
        self.send_header("Location", "/?connected=1")
        self.send_header("Content-Length", "0")
        self.end_headers()


def main():
    # Crash recovery: a job left in running/scanning when the server stopped
    # (crash, restart, deploy) has no live worker. Mark it paused so the
    # user can Resume — already-copied items are never re-uploaded.
    for j in store.list_jobs(limit=200):
        if j["status"] in ("running", "scanning"):
            store.update_job(j["id"], status="paused",
                             error="Server restarted during migration. Press Resume to continue.")
            store.log(j["id"], "warning",
                      "Server stopped mid-migration; job paused. Already-copied files were kept — Resume continues from the last completed item.")
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Drive Migration System running at http://{HOST}:{PORT} (mode={MODE})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
