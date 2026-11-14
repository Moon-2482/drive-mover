"""Persistent storage for migration jobs: SQLite, WAL mode.

Tables:
  jobs   - one row per migration job
  items  - one row per scanned file/folder (the manifest)
  logs   - append-only per-job log lines
  tokens - server-side OAuth tokens (api mode only; never sent to frontend)

No token, secret or credential is ever returned by any query here.
"""
import json
import sqlite3
import threading
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  source_link TEXT, source_id TEXT, source_name TEXT,
  dest_parent_id TEXT, dest_parent_name TEXT, dest_root_id TEXT,
  duplicate_policy TEXT DEFAULT 'rename',
  confirmed_replace INTEGER DEFAULT 0,
  status TEXT DEFAULT 'created',
  total_files INTEGER DEFAULT 0, total_folders INTEGER DEFAULT 0,
  total_bytes INTEGER DEFAULT 0,
  done_files INTEGER DEFAULT 0, done_folders INTEGER DEFAULT 0,
  bytes_done INTEGER DEFAULT 0,
  skipped INTEGER DEFAULT 0, failed INTEGER DEFAULT 0, unsupported INTEGER DEFAULT 0,
  current_file TEXT DEFAULT '',
  scan_error TEXT DEFAULT '', error TEXT DEFAULT '',
  started_at REAL, finished_at REAL,
  created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS items (
  id TEXT PRIMARY KEY,
  job_id TEXT, seq INTEGER,
  source_id TEXT, source_parent_id TEXT,
  dest_parent_source_id TEXT, dest_parent_id TEXT, dest_id TEXT,
  path TEXT, name TEXT, mime TEXT, kind TEXT,
  size INTEGER DEFAULT 0, md5 TEXT DEFAULT '',
  created_time TEXT DEFAULT '', modified_time TEXT DEFAULT '',
  status TEXT DEFAULT 'pending',
  attempts INTEGER DEFAULT 0, method TEXT DEFAULT '', error TEXT DEFAULT '',
  created_at REAL, updated_at REAL,
  UNIQUE(job_id, source_id, path)
);
CREATE INDEX IF NOT EXISTS idx_items_job ON items(job_id, seq);
CREATE TABLE IF NOT EXISTS logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT, ts REAL, level TEXT, message TEXT
);
CREATE TABLE IF NOT EXISTS tokens (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  access_token TEXT, refresh_token TEXT, expires_at REAL,
  scope TEXT, token_type TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, created_at REAL
);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ---- generic helpers ----
    def _exec(self, sql, params=()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _one(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _all(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ---- jobs ----
    def create_job(self, source_link, source_id, dest_parent_id,
                    dest_parent_name, duplicate_policy, confirmed_replace) -> str:
        jid = uuid.uuid4().hex[:16]
        now = time.time()
        self._exec(
            "INSERT INTO jobs(id,source_link,source_id,dest_parent_id,dest_parent_name,"
            " duplicate_policy,confirmed_replace,status,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (jid, source_link, source_id, dest_parent_id, dest_parent_name,
             duplicate_policy, 1 if confirmed_replace else 0, "created", now, now))
        return jid

    def get_job(self, jid):
        r = self._one("SELECT * FROM jobs WHERE id=?", (jid,))
        return dict(r) if r else None

    def list_jobs(self, limit=50):
        return [dict(r) for r in self._all(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))]

    def update_job(self, jid, **fields):
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), jid))

    def refresh_counts(self, jid):
        row = self._one(
            "SELECT "
            " SUM(CASE WHEN kind='file' AND status='done' THEN 1 ELSE 0 END) df,"
            " SUM(CASE WHEN kind='folder' AND status='done' THEN 1 ELSE 0 END) dfo,"
            " SUM(CASE WHEN status='skipped' THEN 1 ELSE 0 END) sk,"
            " SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) fa,"
            " SUM(CASE WHEN status='unsupported' THEN 1 ELSE 0 END) un,"
            " SUM(CASE WHEN kind='file' AND status='done' THEN size ELSE 0 END) bd"
            " FROM items WHERE job_id=?", (jid,))
        d = dict(row) if row else {}
        self.update_job(jid,
                        done_files=d.get("df") or 0,
                        done_folders=d.get("dfo") or 0,
                        skipped=d.get("sk") or 0,
                        failed=d.get("fa") or 0,
                        unsupported=d.get("un") or 0,
                        bytes_done=d.get("bd") or 0)

    # ---- items ----
    def add_item(self, job_id, seq, **f):
        iid = uuid.uuid4().hex[:16]
        now = time.time()
        cols = ["id", "job_id", "seq", "created_at", "updated_at"] + list(f.keys())
        vals = [iid, job_id, seq, now, now] + list(f.values())
        try:
            self._exec(f"INSERT INTO items({','.join(cols)}) VALUES({','.join('?'*len(vals))})", vals)
        except sqlite3.IntegrityError:
            pass  # same (source_id, path) already recorded -> idempotent
        return iid

    def get_items(self, jid, status=None, limit=500, offset=0):
        if status:
            rows = self._all(
                "SELECT * FROM items WHERE job_id=? AND status=? ORDER BY seq LIMIT ? OFFSET ?",
                (jid, status, limit, offset))
        else:
            rows = self._all(
                "SELECT * FROM items WHERE job_id=? ORDER BY seq LIMIT ? OFFSET ?",
                (jid, limit, offset))
        return [dict(r) for r in rows]

    def count_items(self, jid):
        r = self._one("SELECT COUNT(*) c FROM items WHERE job_id=?", (jid,))
        return r["c"] if r else 0

    def get_item(self, iid):
        r = self._one("SELECT * FROM items WHERE id=?", (iid,))
        return dict(r) if r else None

    def update_item(self, iid, **fields):
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE items SET {sets} WHERE id=?", (*fields.values(), iid))

    def find_folder_dest(self, job_id, source_id):
        """dest folder id already created for a scanned source folder id."""
        r = self._one(
            "SELECT dest_id FROM items WHERE job_id=? AND kind='folder'"
            " AND source_id=? AND dest_id IS NOT NULL AND dest_id!='' LIMIT 1",
            (job_id, source_id))
        return r["dest_id"] if r else None

    def pending_items_ordered(self, job_id):
        """Folders first (shallow first), then files — so parents exist."""
        rows = self._all(
            "SELECT * FROM items WHERE job_id=? AND status='pending'"
            " ORDER BY CASE kind WHEN 'folder' THEN 0 ELSE 1 END, seq",
            (job_id,))
        return [dict(r) for r in rows]

    def reset_failed(self, job_id):
        self._exec(
            "UPDATE items SET status='pending', attempts=0, error='' "
            "WHERE job_id=? AND status IN ('failed','unsupported')", (job_id,))

    # ---- logs ----
    def log(self, job_id, level, message):
        with self._lock:
            self._conn.execute(
                "INSERT INTO logs(job_id, ts, level, message) VALUES(?,?,?,?)",
                (job_id, time.time(), level, message[:2000]))
            self._conn.commit()

    def get_logs(self, jid, limit=500):
        rows = self._all(
            "SELECT ts, level, message FROM logs WHERE job_id=? "
            "ORDER BY id DESC LIMIT ?", (jid, limit))
        return [dict(r) for r in reversed(rows)]

    # ---- tokens (server-side only) ----
    def save_token(self, access, refresh, expires_at, scope=""):
        self._exec(
            "INSERT INTO tokens(id, access_token, refresh_token, expires_at, scope, token_type)"
            " VALUES(1,?,?,?,?, 'Bearer') "
            "ON CONFLICT(id) DO UPDATE SET access_token=excluded.access_token,"
            " refresh_token=COALESCE(excluded.refresh_token, refresh_token),"
            " expires_at=excluded.expires_at, scope=excluded.scope",
            (access, refresh, expires_at, scope))

    def get_token(self):
        r = self._one("SELECT * FROM tokens WHERE id=1")
        return dict(r) if r else None

    def create_session(self):
        sid = uuid.uuid4().hex
        self._exec("INSERT INTO sessions(id, created_at) VALUES(?,?)", (sid, time.time()))
        return sid

    def valid_session(self, sid):
        return self._one("SELECT 1 FROM sessions WHERE id=?", (sid,)) is not None
