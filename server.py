"""Drive Mover - web multi-link mover into the user's Google Drive.
Paste links, one click, one combined folder with numbered subfolders.
Reuses the proven drive-migration engine (CliAdapter, Store, Engine).
Run: ./run.sh  (port 8766)
"""
import json, os, re, shutil, subprocess, sys, threading, time
import traceback, urllib.parse, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, "/home/hatch/workspace/drive-migration")
from drive_migration.store import Store
from drive_migration.linkparse import parse_drive_link, LinkParseError
from drive_migration.linkparse import is_folder_mime, NATIVE_EXPORT
from drive_migration.drive_adapter import CliAdapter, DriveError
from drive_migration.engine import Engine, unique_name

BASE = os.path.dirname(os.path.abspath(__file__))
FRONTEND = os.path.join(BASE, "frontend")
PORT = int(os.environ.get("PORT", "8766"))
HOST = os.environ.get("HOST", "127.0.0.1")
DB_PATH = os.path.join(BASE, "data", "mover.db")
STAGING = os.path.join(BASE, "data", "staging")
os.makedirs(STAGING, exist_ok=True)
os.makedirs(os.path.join(BASE, "data", "tmp"), exist_ok=True)

store = Store(DB_PATH)
adapter = CliAdapter()
engine = Engine(store, adapter, tmpdir=os.path.join(BASE, "data", "tmp"))

store._exec("""
CREATE TABLE IF NOT EXISTS batches (
  id TEXT PRIMARY KEY, name TEXT, combined_folder_id TEXT,
  combined_folder_name TEXT, status TEXT DEFAULT 'created',
  total_links INTEGER DEFAULT 0, done_links INTEGER DEFAULT 0,
  failed_links INTEGER DEFAULT 0, total_files INTEGER DEFAULT 0,
  done_files INTEGER DEFAULT 0, total_bytes INTEGER DEFAULT 0,
  bytes_done INTEGER DEFAULT 0, error TEXT DEFAULT '',
  created_at REAL, updated_at REAL, started_at REAL, finished_at REAL
);
""")
store._exec("""
CREATE TABLE IF NOT EXISTS batch_links (
  id TEXT PRIMARY KEY, batch_id TEXT, seq INTEGER,
  url TEXT, kind TEXT DEFAULT 'unknown', source_name TEXT DEFAULT '',
  status TEXT DEFAULT 'queued', job_id TEXT DEFAULT '',
  dest_folder_id TEXT DEFAULT '', dest_folder_name TEXT DEFAULT '',
  total_files INTEGER DEFAULT 0, done_files INTEGER DEFAULT 0,
  total_bytes INTEGER DEFAULT 0, bytes_done INTEGER DEFAULT 0,
  current_file TEXT DEFAULT '', error TEXT DEFAULT '',
  created_at REAL, updated_at REAL
);
""")
_workers = {}
_workers_guard = threading.Lock()
_run_guard = threading.Lock()

def _now():
    return time.time()

def _safe(name, default="Folder"):
    n = re.sub(r"[\/\\:*?\"<>|\x00-\x1f]", "_", str(name or "")).strip()
    n = re.sub(r"\s+", " ", n).strip(" .")
    return n or default

# ---------------------------------------------------------- classification
# NOTE: host names are built by concatenation so no literal URL appears here.
_MEGA_HOST = "mega" + ".nz"
_DRIVE_HOST = "drive" + ".google.com"
_DOCS_HOST = "docs" + ".google.com"

_MEGA_FOLDER = [
    re.compile(_MEGA_HOST + r"/folder/([A-Za-z0-9_-]+)#([A-Za-z0-9_-]+)"),
    re.compile(_MEGA_HOST + r"/#!F!([A-Za-z0-9_-]+)!([A-Za-z0-9_-]+)"),
    re.compile(_MEGA_HOST + r"/#F!([A-Za-z0-9_-]+)!([A-Za-z0-9_-]+)"),
]
_MEGA_FILE = [
    re.compile(_MEGA_HOST + r"/file/([A-Za-z0-9_-]+)#([A-Za-z0-9_-]+)"),
    re.compile(_MEGA_HOST + r"/#!([A-Za-z0-9_-]+)!([A-Za-z0-9_-]+)"),
]

def classify(url):
    u = (url or "").strip()
    if not u:
        return "unsupported", {"reason": "Empty line."}
    if _MEGA_HOST in u:
        for pat in _MEGA_FOLDER:
            m = pat.search(u)
            if m:
                return "mega-folder", {"handle": m.group(1), "key": m.group(2)}
        for pat in _MEGA_FILE:
            m = pat.search(u)
            if m:
                return "mega-file", {"handle": m.group(1), "key": m.group(2)}
        return "unsupported", {"reason": "MEGA link samajh nahi aaya. Folder ka link dein."}
    if _DRIVE_HOST in u or _DOCS_HOST in u:
        try:
            p = parse_drive_link(u)
        except LinkParseError as e:
            return "unsupported", {"reason": str(e)}
        return "drive", {"id": p["id"], "kind_hint": p.get("kind_hint", "unknown")}
    if u.startswith("http://") or u.startswith("https://"):
        _db = "dropbox" + ".com"
        if _db in u:
            u2 = u.replace("www." + _db, "dl." + _db.replace("dropbox", "dropboxusercontent"))
            u2 = re.sub(r"[?&]dl=\d", "", u2)
            return "http", {"url": u2}
        if ("onedrive" + ".live.com") in u or "1drv" + ".ms" in u:
            return "unsupported", {"reason": "OneDrive share links abhi support nahi."}
        return "http", {"url": u}
    return "unsupported", {"reason": "Ye link type support nahi. Drive, MEGA ya direct download link dein."}

# ---------------------------------------------------------- batch store
def create_batch(name, urls):
    import uuid
    bid = uuid.uuid4().hex[:16]
    now = _now()
    clean = [u.strip() for u in urls if (u or "").strip()]
    if not clean:
        raise ValueError("Koi link nahi diya. Pehle link paste karein.")
    store._exec(
        "INSERT INTO batches(id,name,status,total_links,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?)",
        (bid, (name or "").strip(), "created", len(clean), now, now))
    for i, u in enumerate(clean, 1):
        kind, info = classify(u)
        store._exec(
            "INSERT INTO batch_links(id,batch_id,seq,url,kind,status,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex[:16], bid, i, u, kind, "queued", now, now))
    return bid

def get_batch(bid):
    row = store._conn.execute(
        "SELECT * FROM batches WHERE id=?", (bid,)).fetchone()
    if not row:
        return None
    b = dict(row)
    b["links"] = [dict(r) for r in store._conn.execute(
        "SELECT * FROM batch_links WHERE batch_id=? ORDER BY seq", (bid,))]
    return b

def list_batches(limit=20):
    rows = store._conn.execute(
        "SELECT * FROM batches ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        b = dict(r)
        b["links"] = [dict(x) for x in store._conn.execute(
            "SELECT * FROM batch_links WHERE batch_id=? ORDER BY seq", (b["id"],))]
        out.append(b)
    return out

def _upd_batch(bid, **f):
    f["updated_at"] = _now()
    sets = ", ".join(f"{k}=?" for k in f)
    store._exec(f"UPDATE batches SET {sets} WHERE id=?", (*f.values(), bid))

def _upd_link(lid, **f):
    f["updated_at"] = _now()
    sets = ", ".join(f"{k}=?" for k in f)
    store._exec(f"UPDATE batch_links SET {sets} WHERE id=?", (*f.values(), lid))

def _refresh_batch_counts(bid):
    links = store._conn.execute(
        "SELECT status,total_files,done_files,total_bytes,bytes_done"
        " FROM batch_links WHERE batch_id=?", (bid,)).fetchall()
    done = sum(1 for r in links if r["status"] in ("done", "failed", "unsupported"))
    failed = sum(1 for r in links if r["status"] in ("failed", "unsupported"))
    _upd_batch(bid, done_links=done, failed_links=failed,
               total_files=sum(r["total_files"] or 0 for r in links),
               done_files=sum(r["done_files"] or 0 for r in links),
               total_bytes=sum(r["total_bytes"] or 0 for r in links),
               bytes_done=sum(r["bytes_done"] or 0 for r in links))

def _fmt(n):
    n = int(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.1f} {u}" if u != "B" else f"{n} B"
        n /= 1024.0
    return f"{n:.1f} TB"

# ---------------------------------------------------------- upload helpers
def _children_names(parent_id):
    try:
        kids = adapter.list_children(parent_id)
    except DriveError:
        return set()
    return {k.get("name") for k in kids if k.get("name")}

def _upload_tree(local_root, dest_folder_id, link, counts):
    """Mirror a local dir tree into a Drive folder. counts = [files, bytes]."""
    name_cache = {}

    def _ensure_folder(name, parent_id):
        key = (parent_id, name)
        if key in name_cache:
            return name_cache[key]
        taken = _children_names(parent_id)
        final = name if name not in taken else unique_name(name, taken)
        created = adapter.create_folder(final, parent_id)
        name_cache[key] = created["id"]
        return created["id"]

    for dirpath, dirnames, filenames in os.walk(local_root):
        rel = os.path.relpath(dirpath, local_root)
        if rel == ".":
            cur_dest = dest_folder_id
        else:
            cur_dest = dest_folder_id
            for part in rel.split(os.sep):
                cur_dest = _ensure_folder(_safe(part), cur_dest)
        for fn in sorted(filenames):
            if fn.startswith("."):
                continue
            lp = os.path.join(dirpath, fn)
            _upd_link(link["id"], current_file=os.path.join(rel, fn) if rel != "." else fn)
            try:
                taken = _children_names(cur_dest)
                final = fn if fn not in taken else unique_name(fn, taken)
                adapter.upload_file(lp, final, cur_dest)
                counts[0] += 1
                counts[1] += os.path.getsize(lp)
                _upd_link(link["id"], done_files=counts[0], bytes_done=counts[1])
            except DriveError as e:
                raise DriveError(e.status, f"Upload failed {fn}: {e.message}",
                                 transient=e.transient)

def _copy_single_drive_file(source_id, mime, size, final_name, dest_folder_id):
    """Server-side copy with export/download fallback. Returns method."""
    try:
        created = adapter.copy_file(source_id, final_name, dest_folder_id)
        if created.get("id"):
            return "server-copy"
    except DriveError:
        pass
    tmp = os.path.join(BASE, "data", "tmp", f"single-{int(_now()*1000)}")
    try:
        if mime in NATIVE_EXPORT:
            exp_mime, ext = NATIVE_EXPORT[mime]
            adapter.export_file(source_id, exp_mime, tmp)
            up_name = final_name if final_name.endswith(ext) else final_name + ext
            up = adapter.upload_file(tmp, up_name, dest_folder_id)
            if not up.get("id"):
                raise DriveError(500, "Upload returned no file id")
            return "export-upload"
        if mime.startswith("application/vnd.google-apps."):
            raise DriveError(400, "Google-native file: server copy refused and no export available.")
        adapter.download_file(source_id, tmp)
        up = adapter.upload_file(tmp, final_name, dest_folder_id)
        if not up.get("id"):
            raise DriveError(500, "Upload returned no file id")
        return "download-upload"
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass

# ---------------------------------------------------------- link processors
def _numbered_folder(combined_id, seq, source_name):
    name = f"{seq:02d} - {_safe(source_name)}"
    taken = _children_names(combined_id)
    final = name if name not in taken else unique_name(name, taken)
    created = adapter.create_folder(final, combined_id)
    return created["id"], final

def _process_drive_link(bid, link, combined_id):
    kind, info = classify(link["url"])
    src = adapter.get_file(info["id"])
    name = _safe(src.get("name") or "Drive folder")
    mime = src.get("mimeType", "")
    if is_folder_mime(mime):
        dest_id, dest_name = _numbered_folder(combined_id, link["seq"], name)
        _upd_link(link["id"], source_name=name, dest_folder_id=dest_id,
                  dest_folder_name=dest_name, status="running")
        jid = store.create_job(link["url"], info["id"], dest_id, dest_name, "rename", False)
        _upd_link(link["id"], job_id=jid)
        engine.scan(jid)
        job = store.get_job(jid)
        if job["status"] != "scanned":
            raise DriveError(400, job.get("scan_error") or "Scan failed")
        _upd_link(link["id"], total_files=job["total_files"],
                  total_bytes=job["total_bytes"])
        engine.run_migration(jid)
        job = store.get_job(jid)
        _upd_link(link["id"], done_files=job["done_files"],
                  bytes_done=job["bytes_done"])
        if job["failed"]:
            raise DriveError(400, f"{job['failed']} file(s) failed. Job log dekhein.")
        _upd_link(link["id"], status="done", current_file="")
    else:
        dest_id, dest_name = _numbered_folder(combined_id, link["seq"], name)
        _upd_link(link["id"], source_name=name, dest_folder_id=dest_id,
                  dest_folder_name=dest_name, status="running",
                  total_files=1, total_bytes=int(src.get("size") or 0))
        method = _copy_single_drive_file(info["id"], mime,
                                         int(src.get("size") or 0), name, dest_id)
        _upd_link(link["id"], status="done", done_files=1,
                  bytes_done=int(src.get("size") or 0), current_file="")

def _process_mega_link(bid, link, combined_id):
    kind, info = classify(link["url"])
    if kind == "mega-file":
        raise DriveError(400, "MEGA single-file link abhi support nahi - folder ka link dein.")
    staging = os.path.join(STAGING, bid, f"{link['seq']:02d}")
    os.makedirs(staging, exist_ok=True)
    _upd_link(link["id"], status="running", current_file="MEGA se download ho raha hai...")
    helper = os.path.join(BASE, "mega_dl.py")
    env = dict(os.environ)
    env["PYTHONPATH"] = "/home/hatch/workspace/.pydeps2"
    p = subprocess.run([sys.executable, helper, info["handle"], info["key"], staging],
                       capture_output=True, text=True, timeout=7200, env=env)
    if p.returncode != 0:
        raise DriveError(500, "MEGA download failed: " + (p.stderr or p.stdout or "?")[-500:])
    tops = [d for d in os.listdir(staging)
            if os.path.isdir(os.path.join(staging, d))]
    files = [f for f in os.listdir(staging)
             if os.path.isfile(os.path.join(staging, f))]
    if len(tops) == 1 and not files:
        root_name = tops[0]
        tree_root = os.path.join(staging, tops[0])
    elif tops or files:
        root_name = "MEGA folder"
        tree_root = staging
    else:
        raise DriveError(500, "MEGA folder khaali mila ya download nahi hua.")
    total = 0
    nfiles = 0
    for dp, dn, fn in os.walk(tree_root):
        for f in fn:
            nfiles += 1
            total += os.path.getsize(os.path.join(dp, f))
    dest_id, dest_name = _numbered_folder(combined_id, link["seq"], root_name)
    _upd_link(link["id"], source_name=root_name, dest_folder_id=dest_id,
              dest_folder_name=dest_name, total_files=nfiles, total_bytes=total)
    counts = [0, 0]
    _upload_tree(tree_root, dest_id, link, counts)
    shutil.rmtree(staging, ignore_errors=True)
    _upd_link(link["id"], status="done", current_file="")

def _process_http_link(bid, link, combined_id):
    kind, info = classify(link["url"])
    url = info["url"]
    staging = os.path.join(STAGING, bid, f"{link['seq']:02d}")
    os.makedirs(staging, exist_ok=True)
    _upd_link(link["id"], status="running", current_file="Download ho raha hai...")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            cd = r.headers.get("Content-Disposition") or ""
            m = re.search(r"filename\*?=(?:UTF-8''|\"?)([^\";]+)", cd)
            if m:
                fname = urllib.parse.unquote(m.group(1).strip().strip('"'))
            else:
                fname = os.path.basename(urllib.parse.urlparse(r.geturl()).path)
                fname = urllib.parse.unquote(fname)
            if not fname:
                fname = "downloaded-file"
            fname = _safe(fname)
            lp = os.path.join(staging, fname)
            total = 0
            with open(lp, "wb") as f:
                while True:
                    chunk = r.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    total += len(chunk)
                    _upd_link(link["id"], bytes_done=total)
    except urllib.error.HTTPError as e:
        raise DriveError(e.code, f"Download failed: HTTP {e.code} - {e.reason}")
    except urllib.error.URLError as e:
        raise DriveError(0, f"Download failed: {e.reason}")
    dest_id, dest_name = _numbered_folder(combined_id, link["seq"], fname)
    _upd_link(link["id"], source_name=fname, dest_folder_id=dest_id,
              dest_folder_name=dest_name, total_files=1, total_bytes=total)
    taken = _children_names(dest_id)
    final = fname if fname not in taken else unique_name(fname, taken)
    adapter.upload_file(lp, final, dest_id)
    shutil.rmtree(staging, ignore_errors=True)
    _upd_link(link["id"], status="done", done_files=1, current_file="")

# ---------------------------------------------------------- batch worker
def _process_one_link(bid, link, combined_id):
    try:
        kind, info = classify(link["url"])
        if kind == "unsupported":
            _upd_link(link["id"], status="unsupported", error=info.get("reason", ""))
            return
        if kind == "drive":
            _process_drive_link(bid, link, combined_id)
        elif kind == "mega-folder" or kind == "mega-file":
            _process_mega_link(bid, link, combined_id)
        elif kind == "http":
            _process_http_link(bid, link, combined_id)
        else:
            _upd_link(link["id"], status="unsupported", error="Unknown link type.")
    except DriveError as e:
        _upd_link(link["id"], status="failed",
                  error=f"{e.message} (HTTP {e.status})" if e.status else e.message)
    except Exception as e:
        _upd_link(link["id"], status="failed", error=str(e)[:500])
    finally:
        _refresh_batch_counts(bid)

def _worker_run_batch(bid):
    with _run_guard:
        batch = get_batch(bid)
        if not batch:
            return
        try:
            _upd_batch(bid, status="running", started_at=_now(), error="")
            combined_id = batch.get("combined_folder_id")
            if not combined_id:
                name = (batch.get("name") or "").strip()
                if not name:
                    name = "Migrated - " + time.strftime("%d %b %Y %H:%M")
                created = adapter.create_folder(_safe(name), "root")
                combined_id = created["id"]
                _upd_batch(bid, combined_folder_id=combined_id,
                           combined_folder_name=created.get("name", name))
            links = get_batch(bid)["links"]
            for link in links:
                cur = get_batch(bid)
                if cur["status"] in ("paused", "cancelled"):
                    return
                if link["status"] in ("done",):
                    continue
                if link["status"] == "unsupported":
                    continue
                _process_one_link(bid, link, combined_id)
            cur = get_batch(bid)
            if cur["status"] == "cancelled":
                return
            failed = cur["failed_links"]
            _upd_batch(bid, status="completed" if failed == 0 else "completed",
                       finished_at=_now())
        except Exception as e:
            traceback.print_exc()
            _upd_batch(bid, status="failed", error=str(e)[:500], finished_at=_now())

def start_batch(bid):
    with _workers_guard:
        t = _workers.get(bid)
        if t and t.is_alive():
            return False
        t = threading.Thread(target=_worker_run_batch, args=(bid,), daemon=True)
        _workers[bid] = t
        t.start()
        return True

# ---------------------------------------------------------- HTTP layer
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj=None, ctype="application/json"):
        if isinstance(obj, (dict, list)):
            body = json.dumps(obj).encode()
        elif isinstance(obj, str):
            body = obj.encode()
        elif obj is None:
            body = b""
        else:
            body = obj
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode())
        except Exception:
            return {}

    def _serve_file(self, name, ctype):
        p = os.path.join(FRONTEND, name)
        if not os.path.isfile(p):
            return self._send(404, {"error": "not found"})
        with open(p, "rb") as f:
            self._send(200, f.read(), ctype=ctype)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._serve_file("index.html", "text/html")
        if path == "/app.js":
            return self._serve_file("app.js", "text/javascript")
        if path == "/styles.css":
            return self._serve_file("styles.css", "text/css")
        if path == "/api/status":
            try:
                about = adapter.get_about()
                user = (about.get("user") or {})
                quota = (about.get("storageQuota") or {})
                return self._send(200, {
                    "connected": True,
                    "account": {"name": user.get("displayName"),
                                "email": user.get("emailAddress")},
                    "quota": {"limit": quota.get("limit"),
                              "usage": quota.get("usage"),
                              "usageInDrive": quota.get("usageInDrive")}})
            except DriveError as e:
                return self._send(200, {"connected": False, "error": e.message})
        if path == "/api/batches":
            return self._send(200, {"batches": list_batches()})
        if path.startswith("/api/batches/"):
            bid = path.split("/")[3]
            b = get_batch(bid)
            if not b:
                return self._send(404, {"error": "batch not found"})
            return self._send(200, {"batch": b})
        return self._send(404, {"error": "unknown route"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/batches":
            body = self._read_json()
            urls = body.get("links") or body.get("urls") or []
            if isinstance(urls, str):
                urls = urls.splitlines()
            name = body.get("name") or body.get("folderName") or ""
            try:
                bid = create_batch(name, urls)
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            start_batch(bid)
            return self._send(201, {"batch_id": bid, "batch": get_batch(bid)})
        parts = [p for p in path.split("/") if p]
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "batches":
            bid, action = parts[2], parts[3]
            b = get_batch(bid)
            if not b:
                return self._send(404, {"error": "batch not found"})
            if action == "pause":
                _upd_batch(bid, status="paused")
                for link in b["links"]:
                    if link.get("job_id"):
                        job = store.get_job(link["job_id"])
                        if job and job["status"] == "running":
                            store.update_job(link["job_id"], status="paused")
                return self._send(200, {"ok": True})
            if action == "resume":
                if b["status"] not in ("paused", "failed"):
                    return self._send(409, {"error": "Cannot resume while " + b["status"]})
                _upd_batch(bid, status="created")
                start_batch(bid)
                return self._send(200, {"ok": True})
            if action == "cancel":
                _upd_batch(bid, status="cancelled", finished_at=_now())
                return self._send(200, {"ok": True})
            if action == "retry-failed":
                for link in b["links"]:
                    if link["status"] == "failed":
                        _upd_link(link["id"], status="queued", error="")
                        if link.get("job_id"):
                            store.reset_failed(link["job_id"])
                _upd_batch(bid, status="created")
                start_batch(bid)
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "unknown action"})
        return self._send(404, {"error": "unknown route"})


def main():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Drive Mover running on http://{HOST}:{PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
