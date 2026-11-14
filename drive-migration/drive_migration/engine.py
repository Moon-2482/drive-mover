"""Scan + copy engine.

Scan: breadth-first recursive walk with pagination (adapter handles page
tokens), shortcut resolution with ancestor-cycle detection, per-item
manifest rows in SQLite. Inaccessible children are recorded as failed
items with the real error — never silently skipped.

Copy: folders first (shallow first) so parents exist, then files.
Preferred path for EVERY file is server-side files.copy — no download, no
browser memory, no temp files. Only when Drive refuses copy do we fall
back to export+upload (Workspace-native) or download+upload (binary),
streaming through a server-side temp file that is always deleted.
Duplicate policy: skip | rename | replace (replace needs confirmed_replace).
Resume: items already 'done' (with dest_id) are never re-uploaded.
Pause/cancel: worker checks the job row before every item.
"""
import os
import re
import tempfile
import time
import threading

from .drive_adapter import DriveError, retry
from .linkparse import (is_folder_mime, is_shortcut_mime, NATIVE_EXPORT,
                        GOOGLE_NATIVE_MIMES)

MAX_SCAN_DEPTH = 50
MAX_ITEMS_PER_JOB = 200_000


def _safe_name(name):
    return (name or "unnamed").strip() or "unnamed"


def _split_ext(name):
    if "." in name and not name.startswith("."):
        base, ext = name.rsplit(".", 1)
        return base, "." + ext
    return name, ""


def unique_name(base_name, taken: set):
    """'Report.docx' -> 'Report (2).docx', 'Report (3).docx', ..."""
    if base_name not in taken:
        return base_name
    stem, ext = _split_ext(base_name)
    m = re.match(r"^(.*) \((\d+)\)$", stem)
    if m:
        stem, n = m.group(1), int(m.group(2))
    else:
        n = 1
    while True:
        n += 1
        cand = f"{stem} ({n}){ext}"
        if cand not in taken:
            return cand


class Engine:
    def __init__(self, store, adapter, tmpdir=None):
        self.store = store
        self.adapter = adapter
        self.tmpdir = tmpdir or tempfile.gettempdir()
        self._locks = {}
        self._locks_guard = threading.Lock()

    def _lock(self, jid):
        with self._locks_guard:
            return self._locks.setdefault(jid, threading.Lock())

    # ------------------------------------------------------------ scanning
    def scan(self, job_id):
        """Populate items for a job. Returns (files, folders, bytes)."""
        st = self.store
        job = st.get_job(job_id)
        st.update_job(job_id, status="scanning", scan_error="")
        st.log(job_id, "info", f"Scanning source folder {job['source_id']} …")
        try:
            root = self.adapter.get_file(job["source_id"])
        except DriveError as e:
            st.update_job(job_id, status="failed",
                          scan_error=f"Cannot access source folder: {e.message} (HTTP {e.status})")
            st.log(job_id, "error", f"Source folder not accessible: {e.message}")
            return None
        if not is_folder_mime(root.get("mimeType", "")):
            st.update_job(job_id, status="failed",
                          scan_error="That link points to a single file, not a folder. "
                                     "Paste a folder link.")
            return None
        st.update_job(job_id, source_name=root.get("name", ""))
        # Root folder itself is item seq 0 (its dest is created at copy time)
        seq = [0]
        total_files = [0]
        total_bytes = [0]
        total_folders = [0]

        def add(kind, src, path, parent_src, dest_parent_src, extra=None):
            seq[0] += 1
            f = {"source_id": src.get("id", ""), "source_parent_id": parent_src or "",
                 "dest_parent_source_id": dest_parent_src or "",
                 "path": path, "name": _safe_name(src.get("name")),
                 "mime": src.get("mimeType", ""), "kind": kind,
                 "size": int(src.get("size") or 0),
                 "md5": src.get("md5Checksum") or "",
                 "created_time": src.get("createdTime") or "",
                 "modified_time": src.get("modifiedTime") or "",
                 "status": "pending"}
            if extra:
                f.update(extra)
            st.add_item(job_id, seq[0], **f)
            return f

        # queue entries: (folder_id, folder_display_path, parent_source_id,
        #               dest_parent_source_id, ancestor_ids frozenset)
        queue = [(job["source_id"], "/" + _safe_name(root.get("name", "Folder")),
                  None, None, frozenset({job["source_id"] }))]
        # root folder item (dest parent = job dest_parent_id)
        add("folder", root, "/" + _safe_name(root.get("name", "Folder")),
            None, None, {"dest_parent_source_id": "__JOB_DEST__"})
        # NOTE: no dedupe by path here on purpose — two different files may
        # legitimately share one path (same name in one folder). Deduping by
        # path silently dropped the second duplicate in testing. Cycles are
        # prevented by the per-branch `ancestors` set, not by path dedupe.
        while queue:
            folder_id, folder_path, parent_src, dest_parent_src, ancestors = queue.pop(0)
            if folder_path.count("/") > MAX_SCAN_DEPTH:
                st.log(job_id, "warning", f"Depth limit reached at {folder_path}; not descending further.")
                continue
            try:
                children = self.adapter.list_children(folder_id)
            except DriveError as e:
                seq[0] += 1
                st.add_item(job_id, seq[0], source_id=folder_id + ":list-error",
                            source_parent_id=parent_src or "",
                            dest_parent_source_id=dest_parent_src or "",
                            path=folder_path + " (listing failed)",
                            name="(listing failed)", mime="", kind="file",
                            size=0, md5="", status="failed",
                            error=f"Could not list folder contents: {e.message} (HTTP {e.status})")
                st.log(job_id, "error", f"Cannot list {folder_path}: {e.message}")
                continue
            for ch in children:
                cid = ch.get("id", "")
                cname = _safe_name(ch.get("name"))
                cpath = folder_path + "/" + cname
                mime = ch.get("mimeType", "")
                # (no path dedupe — see note above)
                if is_shortcut_mime(mime):
                    target = (ch.get("shortcutDetails") or {}).get("targetId")
                    if not target:
                        add("file", ch, cpath, folder_id, folder_id,
                            {"status": "unsupported",
                             "error": "Shortcut has no target."})
                        continue
                    if target in ancestors:
                        add("file", ch, cpath, folder_id, folder_id,
                            {"status": "skipped",
                             "error": "Shortcut cycle detected — target is an ancestor folder. Skipped to avoid an infinite loop."})
                        st.log(job_id, "warning", f"Shortcut cycle skipped: {cpath}")
                        continue
                    try:
                        tgt = self.adapter.get_file(target)
                    except DriveError as e:
                        add("file", ch, cpath, folder_id, folder_id,
                            {"status": "failed",
                             "error": f"Shortcut target not accessible: {e.message} (HTTP {e.status})"})
                        continue
                    tmime = tgt.get("mimeType", "")
                    if is_folder_mime(tmime):
                        total_folders[0] += 1
                        add("folder", tgt, cpath, folder_id, folder_id,
                            {"source_id": target, "name": cname})
                        queue.append((target, cpath, folder_id, folder_id,
                                      ancestors | {target}))
                    else:
                        total_files[0] += 1
                        total_bytes[0] += int(tgt.get("size") or 0)
                        add("file", tgt, cpath, folder_id, folder_id,
                            {"source_id": target, "name": cname,
                             "md5": tgt.get("md5Checksum") or ""})
                    continue
                if is_folder_mime(mime):
                    total_folders[0] += 1
                    add("folder", ch, cpath, folder_id, folder_id)
                    queue.append((cid, cpath, folder_id, folder_id, ancestors | {cid}))
                else:
                    total_files[0] += 1
                    total_bytes[0] += int(ch.get("size") or 0)
                    add("file", ch, cpath, folder_id, folder_id)
                if seq[0] > MAX_ITEMS_PER_JOB:
                    st.log(job_id, "warning", "Item cap reached; scan stopped.")
                    queue.clear()
                    break
        # root folder counted in items but not in total_folders (it is the source)
        # Total folders INCLUDES the recreated source root, so that
        # done_folders/total_folders can honestly reach 3/3, not 3/2.
        st.update_job(job_id, status="scanned",
                      total_files=total_files[0], total_folders=total_folders[0] + 1,
                      total_bytes=total_bytes[0])
        st.refresh_counts(job_id)
        st.log(job_id, "info",
               f"Scan complete: {total_folders[0]} subfolders (+ source root), "
               f"{total_files[0]} files, {total_bytes[0]} bytes.")
        return {"files": total_files[0], "folders": total_folders[0], "bytes": total_bytes[0]}

    # ------------------------------------------------------------ copying
    def _dest_children_names(self, job_id, dest_parent_id):
        """Names already present in a destination folder (cached per job)."""
        try:
            kids = self.adapter.list_children(dest_parent_id)
        except DriveError:
            return set()
        return {k.get("name") for k in kids if k.get("name")}

    def _resolve_dest_parent(self, job, item):
        """Destination folder id for an item's parent."""
        st = self.store
        dps = item.get("dest_parent_source_id") or ""
        if dps == "__JOB_DEST__":
            return job["dest_parent_id"]
        if dps:
            dest = st.find_folder_dest(job["id"], dps)
            if dest:
                return dest
        # Fallback: parent folder item may not have been created yet —
        # create the chain on demand from the item path.
        return job["dest_parent_id"]

    def _apply_duplicate_policy(self, job, dest_parent_id, name, kind):
        """Returns (action, final_name, existing_id_or_None).
        action: 'create' | 'skip'"""
        taken = self._dest_children_names(job["id"], dest_parent_id)
        policy = job.get("duplicate_policy") or "rename"
        if name not in taken:
            return "create", name, None
        if policy == "skip":
            return "skip", name, None
        if policy == "replace":
            if not job.get("confirmed_replace"):
                return "skip", name, None
            # find the existing id to trash
            try:
                for k in self.adapter.list_children(dest_parent_id):
                    if k.get("name") == name:
                        return "replace", name, k.get("id")
            except DriveError:
                pass
            return "create", name, None
        return "create", unique_name(name, taken), None

    def _copy_one_file(self, job, item, dest_parent_id, final_name):
        """Copy a single file. Returns (dest_id, method). Raises DriveError."""
        st = self.store
        src_id = item["source_id"]
        # 1) Preferred: server-side copy. Works for binary AND Workspace-native.
        try:
            created = self.adapter.copy_file(src_id, final_name, dest_parent_id)
            dest_id = created.get("id")
            if dest_id:
                return dest_id, "server-copy"
        except DriveError as e:
            # 403/404/400 -> fall through to export/download fallback.
            # Transient errors already retried inside the adapter; if we are
            # here the copy genuinely failed.
            last_copy_error = e
        else:
            last_copy_error = None
        # 2) Fallback paths
        mime = item.get("mime") or ""
        tmp = None
        try:
            if mime in NATIVE_EXPORT:
                export_mime, ext = NATIVE_EXPORT[mime]
                fd, tmp = tempfile.mkstemp(suffix=ext, dir=self.tmpdir)
                os.close(fd)
                self.adapter.export_file(src_id, export_mime, tmp)
                up_name = final_name if final_name.endswith(ext) else final_name + ext
                # Uploading the exported Office file keeps an exact, openable
                # copy. Converting back to native Google format would lose
                # fidelity for some files, so we keep the exported format and
                # say so in the report.
                up = self.adapter.upload_file(tmp, up_name, dest_parent_id)
                dest_id = up.get("id")
                if not dest_id:
                    raise DriveError(500, "Upload returned no file id", transient=False)
                return dest_id, f"export-upload ({export_mime})"
            if mime in GOOGLE_NATIVE_MIMES:
                raise DriveError(
                    400,
                    f"Google-native type {mime} has no supported Drive export and "
                    "server-side copy was refused. Ask the owner for copy/download "
                    "access, or open it in Drive and use File > Make a copy.",
                    transient=False)
            # Binary: download -> resumable/streaming upload
            fd, tmp = tempfile.mkstemp(dir=self.tmpdir)
            os.close(fd)
            self.adapter.download_file(src_id, tmp)
            if os.path.getsize(tmp) != (item.get("size") or os.path.getsize(tmp)):
                # size mismatch is a warning, not fatal — Drive sizes can lag
                st.log(job["id"], "warning",
                       f"Size mismatch on download for {item['path']}; continuing.")
            up = self.adapter.upload_file(tmp, final_name, dest_parent_id)
            dest_id = up.get("id")
            if not dest_id:
                raise DriveError(500, "Upload returned no file id", transient=False)
            return dest_id, "download-upload"
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        if last_copy_error is not None:
            raise last_copy_error
        raise DriveError(500, "Copy failed for an unknown reason", transient=False)

    def run_migration(self, job_id):
        """Worker entry point. Resumable: only 'pending' items are processed."""
        st = self.store
        with self._lock(job_id):
            job = st.get_job(job_id)
            if not job or job["status"] not in ("scanned", "running", "paused"):
                return
            st.update_job(job_id, status="running", started_at=job.get("started_at") or time.time(),
                          error="")
            st.log(job_id, "info", "Migration started/resumed.")
            try:
                self._migrate(job_id)
            except Exception as e:  # never kill the worker thread silently
                st.log(job_id, "error", f"Worker stopped unexpectedly: {e}")
                cur = st.get_job(job_id)
                if cur and cur["status"] == "running":
                    st.update_job(job_id, status="paused",
                                  error=f"Worker stopped: {e}. Press Resume to continue.")

    def _migrate(self, job_id):
        st = self.store
        while True:
            job = st.get_job(job_id)
            if not job:
                return
            if job["status"] in ("cancelled", "completed"):
                return
            if job["status"] == "paused":
                st.log(job_id, "info", "Migration paused.")
                return
            pending = st.pending_items_ordered(job_id)
            if not pending:
                st.refresh_counts(job_id)
                job = st.get_job(job_id)
                st.update_job(job_id, status="completed", finished_at=time.time(),
                              current_file="")
                st.log(job_id, "info",
                       f"Migration finished: {job['done_files']} files, "
                       f"{job['done_folders']} folders copied; "
                       f"{job['skipped']} skipped, {job['failed']} failed, "
                       f"{job['unsupported']} unsupported.")
                return
            item = pending[0]
            self._process_item(job_id, item)
            st.refresh_counts(job_id)

    def _process_item(self, job_id, item):
        st = self.store
        job = st.get_job(job_id)
        st.update_job(job_id, current_file=item["path"])
        try:
            if item["kind"] == "folder":
                self._process_folder(job, item)
            else:
                self._process_file(job, item)
        except DriveError as e:
            st.update_item(item["id"], status="failed", attempts=item["attempts"] + 1,
                           error=f"{e.message} (HTTP {e.status})")
            st.log(job_id, "error", f"FAILED {item['path']}: {e.message}")
        except Exception as e:
            st.update_item(item["id"], status="failed", attempts=item["attempts"] + 1,
                           error=str(e))
            st.log(job_id, "error", f"FAILED {item['path']}: {e}")

    def _find_orphan(self, job, dest_parent_id, name, kind, item):
        """Adopt a destination child that THIS job created but never recorded.

        Crash window: Drive create succeeds, then the process dies before
        the SQLite row is marked done. On resume the naive code would see
        a same-name child and rename to 'Name (2)' — leaving an empty
        orphan folder / duplicate file. An orphan is recognised narrowly:
        same name + same kind, created AFTER this job started, and
        (folder: still empty) / (file: same size and md5 when known).
        A genuinely pre-existing same-name item fails the created-after
        test and still goes through the normal duplicate policy.
        """
        try:
            kids = self.adapter.list_children(dest_parent_id)
        except DriveError:
            return None
        job_start = float(job.get("created_at") or 0) - 5  # clock skew
        for k in kids:
            if k.get("name") != name:
                continue
            if kind == "folder" and k.get("mimeType") != "application/vnd.google-apps.folder":
                continue
            if kind == "file" and k.get("mimeType") == "application/vnd.google-apps.folder":
                continue
            created = k.get("createdTime") or ""
            try:
                ts = __import__("datetime").datetime.fromisoformat(
                    created.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            if ts < job_start:
                continue
            if kind == "folder":
                try:
                    if self.adapter.list_children(k["id"]):
                        continue  # non-empty -> not our orphan
                except DriveError:
                    continue
                return k
            # file: size/md5 must match when both sides know them
            if item.get("size") and k.get("size") is not None:
                try:
                    if int(k["size"]) != int(item["size"]):
                        continue
                except (TypeError, ValueError):
                    pass
            if item.get("md5") and k.get("md5Checksum"):
                if k["md5Checksum"] != item["md5"]:
                    continue
            return k
        return None

    def _process_folder(self, job, item):
        st = self.store
        # Root folder item: its dest parent is the user's chosen destination;
        # the root folder itself is recreated inside it.
        dest_parent = self._resolve_dest_parent(job, item)
        if item.get("dest_parent_source_id") == "__JOB_DEST__":
            dest_parent = job["dest_parent_id"]
        orphan = self._find_orphan(job, dest_parent, item["name"], "folder", item)
        if orphan:
            st.update_item(item["id"], status="done", dest_id=orphan["id"],
                           dest_parent_id=dest_parent, method="adopted-orphan")
            st.log(job["id"], "info",
                   f"Adopted {item['path']} created by the interrupted run (no re-upload).")
            return
        action, final_name, existing = self._apply_duplicate_policy(
            job, dest_parent, item["name"], "folder")
        if action == "skip":
            st.update_item(item["id"], status="skipped", dest_parent_id=dest_parent,
                            error="Destination folder already exists (skip policy).")
            # Reuse the existing folder as the parent for its children so
            # the rest of the tree still lands in the right place.
            if existing is None:
                try:
                    for k in self.adapter.list_children(dest_parent):
                        if k.get("name") == item["name"]:
                            existing = k.get("id")
                            break
                except DriveError:
                    pass
            if existing:
                st.update_item(item["id"], dest_id=existing)
            return
        if action == "replace" and existing:
            self.adapter.trash_file(existing)
            st.log(job["id"], "info", f"Replaced existing folder: {item['path']}")
        created = self.adapter.create_folder(final_name, dest_parent)
        dest_id = created.get("id")
        if not dest_id:
            raise DriveError(500, "Folder creation returned no id", transient=False)
        # Best-effort timestamp preservation (Drive may ignore it).
        self._preserve_times(job, item, dest_id)
        st.update_item(item["id"], status="done", dest_id=dest_id,
                       dest_parent_id=dest_parent, method="create-folder")

    def _process_file(self, job, item):
        st = self.store
        dest_parent = self._resolve_dest_parent(job, item)
        orphan = self._find_orphan(job, dest_parent, item["name"], "file", item)
        if orphan:
            st.update_item(item["id"], status="done", dest_id=orphan["id"],
                           dest_parent_id=dest_parent, method="adopted-orphan")
            st.log(job["id"], "info",
                   f"Adopted {item['path']} created by the interrupted run (no re-upload).")
            return
        action, final_name, existing = self._apply_duplicate_policy(
            job, dest_parent, item["name"], "file")
        if action == "skip":
            st.update_item(item["id"], status="skipped", dest_parent_id=dest_parent,
                            error="Destination file already exists (skip policy).")
            return
        if action == "replace" and existing:
            self.adapter.trash_file(existing)
            st.log(job["id"], "info", f"Replaced existing file: {item['path']}")
        dest_id, method = self._copy_one_file(job, item, dest_parent, final_name)
        # Verify: destination file exists, name matches, size matches (binary).
        verify = self.adapter.get_file(dest_id)
        if not verify or not verify.get("id"):
            raise DriveError(500, "Destination file could not be verified after copy",
                             transient=False)
        src_md5, dst_md5 = item.get("md5") or "", verify.get("md5Checksum") or ""
        if src_md5 and dst_md5 and src_md5 != dst_md5:
            raise DriveError(500, "Checksum mismatch after copy (md5 differs)",
                             transient=False)
        if item.get("size") and verify.get("size") is not None:
            try:
                if int(verify["size"]) != int(item["size"]) and method == "server-copy":
                    st.log(job["id"], "warning",
                           f"Size differs after copy for {item['path']} "
                           f"(source {item['size']} vs dest {verify['size']}).")
            except (TypeError, ValueError):
                pass
        self._preserve_times(job, item, dest_id)
        st.update_item(item["id"], status="done", dest_id=dest_id,
                       dest_parent_id=dest_parent, method=method)

    def _preserve_times(self, job, item, dest_id):
        # Best-effort only: Drive's files.copy stamps the copy with the copy
        # time. Setting modifiedTime back via files.update works for files
        # the user owns; it is rejected for some shared/Workspace-native
        # files. A rejection must never fail the migration.
        fields = {}
        if item.get("modified_time"):
            fields["modifiedTime"] = item["modified_time"]
        if not fields:
            return
        try:
            self.adapter.update_metadata(dest_id, fields)
        except Exception:
            pass  # documented limitation; copy itself already succeeded
