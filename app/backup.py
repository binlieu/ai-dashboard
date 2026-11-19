"""Local fragmented MP4 recording and retention for marked folders."""

from collections import deque
from contextlib import ExitStack, contextmanager, nullcontext
import ctypes
import datetime
import errno
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import sys
import threading
import time
import unicodedata

from .security import CAMERA_ID

DAYS = (1, 7, 30)
SEGMENT_SECONDS = 180
MARKER = ".kasa-dashboard-backup"
MIN_FREE_BYTES = 5 * 1024**3
BACKUP_DEFAULTS = {"folder": "", "cameras": {}}
MAX_BOX_BYTES = 64 * 1024**2
STORAGE_RETRY_SECONDS = 30
STOP_SECONDS = 5
DATE_NAME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
SEGMENT_NAME = re.compile(r"([0-9]{2}-[0-9]{2}-[0-9]{2})\.mp4(?:\.part)?")
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL
MESSAGES = {
    "invalid_settings": "Send a folder and valid retention settings for saved cameras.",
    "invalid_folder": "Choose an absolute path to a backup folder.",
    "folder_missing": "That folder does not exist. Check that the drive is connected.",
    "folder_forbidden": "Choose a folder outside the dashboard project.",
    "folder_readonly": "The dashboard cannot write to that folder.",
    "invalid_camera": "Choose a saved camera.",
    "low_space": "The backup drive is low on space.",
    "stream": "The camera did not provide a usable MP4 stream.",
}


class BackupError(ValueError):
    def __init__(self, code):
        self.code = code
        self.message = MESSAGES[code]
        super().__init__(self.message)


def valid_folder_text(text):
    return isinstance(text, str) and (not text or (
        len(text) <= 1024 and os.path.isabs(text) and
        not any(unicodedata.category(char) in ("Cc", "Cs") for char in text)))


def validate_folder(text, forbidden=()):
    if not valid_folder_text(text):
        raise BackupError("invalid_folder")
    if not text:
        return ""
    try:
        folder = Path(text).resolve()
        if folder == folder.parent:
            raise BackupError("invalid_folder")
        for item in forbidden:
            blocked = Path(item).resolve()
            if folder.is_relative_to(blocked) or blocked.is_relative_to(folder):
                raise BackupError("folder_forbidden")
        if not folder.is_dir():
            raise BackupError("folder_missing")
    except PermissionError:
        raise BackupError("folder_readonly") from None
    except (OSError, RuntimeError):
        raise BackupError("folder_missing") from None
    if not os.access(folder, os.W_OK | os.X_OK):
        raise BackupError("folder_readonly")
    return str(folder)


def valid_policies(policies, days=DAYS):
    return isinstance(policies, dict) and all(
        isinstance(camera_id, str) and CAMERA_ID.fullmatch(camera_id) and
        type(value) is int and value in days for camera_id, value in policies.items())


def segment_path(root, camera_id, when):
    if not isinstance(camera_id, str) or not CAMERA_ID.fullmatch(camera_id):
        raise BackupError("invalid_camera")
    return Path(root) / camera_id / when.date().isoformat() / (when.strftime("%H-%M-%S") + ".mp4")


def _box_size(data, offset):
    if len(data) - offset < 8:
        return 0
    size, kind = struct.unpack_from(">I4s", data, offset)
    if not all(32 <= byte <= 126 for byte in kind):
        raise BackupError("stream")
    header = 8
    if size == 1:
        header = 16
        if len(data) - offset < header:
            return 0
        size, = struct.unpack_from(">Q", data, offset + 8)
    if not header <= size <= MAX_BOX_BYTES:
        raise BackupError("stream")
    return size if len(data) - offset >= size else 0


def split_boxes(data):
    offset = 0
    while True:
        size = _box_size(data, offset)
        if not size:
            return offset
        offset += size


@contextmanager
def _directory(path, parent=None, create=False):
    if create:
        try:
            os.mkdir(path, mode=0o700, dir_fd=parent)
        except FileExistsError:
            pass
    descriptor = os.open(path, DIRECTORY_FLAGS, dir_fd=parent)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _regular_file(parent, name):
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except OSError:
        return None
    return info if stat.S_ISREG(info.st_mode) else None


def _exists(parent, name):
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _remove_companion(parent, name):
    companion = "._" + name
    if _regular_file(parent, companion) is not None:
        try:
            os.unlink(companion, dir_fd=parent)
        except OSError:
            pass


def _unlink_recording(parent, name):
    os.unlink(name, dir_fd=parent)
    _remove_companion(parent, name)


def _dates(root_fd, cameras):
    for camera_id in cameras:
        try:
            with _directory(camera_id, root_fd) as camera_fd, os.scandir(camera_fd) as entries:
                for entry in entries:
                    if DATE_NAME.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False):
                        try:
                            day = datetime.date.fromisoformat(entry.name)
                        except ValueError:
                            continue
                        yield camera_id, camera_fd, day
        except OSError:
            continue


def _segment_time(name):
    match = SEGMENT_NAME.fullmatch(name)
    if match:
        try:
            return datetime.time(*map(int, match[1].split("-")))
        except ValueError:
            pass
    return None


def _scan_date(camera_fd, day, tzinfo=None):
    try:
        with _directory(day.isoformat(), camera_fd) as date_fd, os.scandir(date_fd) as entries:
            for entry in entries:
                clock_time = _segment_time(entry.name)
                if clock_time is None:
                    continue
                info = _regular_file(date_fd, entry.name)
                if info is not None:
                    when = datetime.datetime.combine(day, clock_time, tzinfo=tzinfo)
                    yield entry.name, when, entry.name.endswith(".part"), info
    except OSError:
        return


def _remove_empty_date(camera_fd, name):
    with _directory(name, camera_fd) as date_fd:
        companions = []
        with os.scandir(date_fd) as entries:
            for entry in entries:
                if (not entry.name.startswith("._") or _segment_time(entry.name[2:]) is None or
                        not entry.is_file(follow_symlinks=False)):
                    return
                companions.append(entry.name[2:])
        for recording in companions:
            if not _exists(date_fd, recording):
                _remove_companion(date_fd, recording)
        # macOS rmdir can discard metadata entries itself, even ._ symlinks.
        with os.scandir(date_fd) as entries:
            if next(entries, None) is None:
                os.rmdir(name, dir_fd=camera_fd)


def prune(root, policies, now, free_space, min_free, active=()):
    if not valid_policies(policies) or not policies:
        return []
    try:
        root = Path(root)
        with _directory(root) as root_fd:
            if _regular_file(root_fd, MARKER) is None:
                return []
            removed, _ = _prune(root_fd, root, policies, now, free_space, min_free, set(map(Path, active)))
            return removed
    except (OSError, TypeError, ValueError):
        return []


def _prune(root_fd, root, policies, now, free_space, min_free, active, guard=None):
    segments, partials, dates, removed = [], [], [], []
    used = 0
    for camera_id, camera_fd, day in _dates(root_fd, policies):
        date = root / camera_id / day.isoformat()
        dates.append(date)
        for name, when, partial, info in _scan_date(camera_fd, day, now.tzinfo):
            candidate = (when, date / name, info)
            (partials if partial else segments).append(candidate)
            if not partial:
                used += info.st_size

    def remove(path, original, segment_size=0):
        nonlocal used
        camera_id = path.parent.parent.name
        try:
            with guard(camera_id) if guard else nullcontext(True) as allowed:
                if not allowed or path in active:
                    return
                with _directory(camera_id, root_fd) as camera_fd:
                    with _directory(path.parent.name, camera_fd) as date_fd:
                        info = _regular_file(date_fd, path.name)
                        if info is not None and os.path.samestat(original, info):
                            _unlink_recording(date_fd, path.name)
                            removed.append(path)
                            used -= segment_size
        except OSError:
            pass

    expiry = {camera_id: now - datetime.timedelta(days=days) for camera_id, days in policies.items()}
    low_space_cutoff = now - datetime.timedelta(hours=24)
    remaining = []
    segments.sort(key=lambda item: item[:2])
    for when, path, info in segments:
        if when < expiry[path.parent.parent.name]:
            remove(path, info, info.st_size)
        elif when < low_space_cutoff:
            remaining.append((path, info))
    partial_cutoff = now - datetime.timedelta(hours=1)
    partials.sort(key=lambda item: item[:2])
    for when, path, info in partials:
        if when < partial_cutoff:
            remove(path, info)
    for path, info in remaining:
        if path in active:
            continue
        try:
            if free_space() >= min_free:
                break
        except Exception:
            break
        remove(path, info, info.st_size)
    for date in dates:
        camera_id = date.parent.name
        try:
            with guard(camera_id) if guard else nullcontext(True) as allowed:
                if allowed:
                    with _directory(camera_id, root_fd) as camera_fd:
                        _remove_empty_date(camera_fd, date.name)
        except OSError:
            pass
    return removed, used


def _publish(partial, final, source_fd, destination_fd):
    # Prefer atomic no-overwrite operations. FAT and exFAT may support neither.
    libc = ctypes.CDLL(None, use_errno=True)
    symbol, flag = ("renameatx_np", 4) if sys.platform == "darwin" else ("renameat2", 1)
    rename = getattr(libc, symbol, None)
    if rename is not None:
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        if rename(source_fd, os.fsencode(partial), destination_fd, os.fsencode(final), flag) == 0:
            _remove_companion(source_fd, partial)
            return True
        error = ctypes.get_errno()
        if error not in (errno.ENOSYS, errno.ENOTSUP, errno.EINVAL):
            raise OSError(error, "Could not finalize recording")
    try:
        os.link(partial, final, src_dir_fd=source_fd, dst_dir_fd=destination_fd, follow_symlinks=False)
    except OSError as error:
        if error.errno not in (errno.ENOTSUP, errno.EPERM, errno.EOPNOTSUPP, errno.EINVAL):
            raise
        if _exists(destination_fd, final):
            return False
        os.rename(partial, final, src_dir_fd=source_fd, dst_dir_fd=destination_fd)
        _remove_companion(source_fd, partial)
    else:
        _unlink_recording(source_fd, partial)
    return True


class _Segment:
    def __init__(self, root, camera_id, when, root_fd, header):
        while True:
            path = segment_path(root, camera_id, when)
            partial = path.with_suffix(".mp4.part")
            with ExitStack() as directories:
                camera_fd = directories.enter_context(_directory(camera_id, root_fd, create=True))
                date_fd = directories.enter_context(_directory(path.parent.name, camera_fd, create=True))
                file = self._claim(date_fd, path.name, partial.name)
                if file is not None:
                    try:
                        self._identity = os.fstat(file.fileno())
                    except OSError:
                        file.close()
                        raise
                    self._directories = directories.pop_all()
                    self._camera_fd, self._date_fd = camera_fd, date_fd
                    self._file = file
                    self.path, self.partial = path, partial
                    self._when = when
                    break
            when += datetime.timedelta(seconds=1)
        self._pending = header
        self._has_moof = False
        self._good_size = 0

    @staticmethod
    def _claim(parent, final, partial):
        if _exists(parent, final):
            return None
        try:
            fd = os.open(partial, CREATE_FLAGS, 0o600, dir_fd=parent)
        except FileExistsError:
            return None
        try:
            # Another recorder may have just finalized this second.
            if not _exists(parent, final):
                return os.fdopen(fd, "wb", buffering=0)
            _unlink_recording(parent, partial)
        except BaseException:
            os.close(fd)
            raise
        os.close(fd)
        return None

    def _write(self, data):
        remaining = memoryview(data)
        while remaining:
            written = self._file.write(remaining)
            if not written:
                raise OSError("Recording write made no progress")
            remaining = remaining[written:]

    def box(self, box):
        kind = box[4:8]
        if kind == b"mdat":
            if not self._has_moof:
                raise BackupError("stream")
            self._write(self._pending)
            self._write(box)
            self._good_size = self._file.tell()
            self._pending.clear()
            self._has_moof = False
            return True
        if kind == b"moof":
            if self._has_moof:
                raise BackupError("stream")
            self._has_moof = True
        if len(self._pending) + len(box) > 2 * MAX_BOX_BYTES:
            raise BackupError("stream")
        self._pending.extend(box)
        return False

    def _refresh_identity(self):
        # exFAT reports a placeholder inode for a new empty file; it changes once data is
        # written, so identify the partial from the still-open descriptor, not from creation.
        try:
            self._identity = os.fstat(self._file.fileno())
        except (OSError, ValueError):
            pass

    def _owns_partial(self):
        partial = _regular_file(self._date_fd, self.partial.name)
        if partial is None:
            return False
        if os.path.samestat(self._identity, partial):
            return True
        # exFAT can report a placeholder inode and a stale modification time through the open
        # descriptor while the path shows the real ones (measured on macOS); only the device
        # and the size agree there, so fall back to those.
        return self._identity.st_dev == partial.st_dev and self._identity.st_size == partial.st_size

    @property
    def has_video(self):
        return self._good_size > 0

    def finish(self):
        try:
            if not self._good_size:
                try:
                    self._refresh_identity()
                    self._file.close()
                finally:
                    if self._owns_partial():
                        _unlink_recording(self._date_fd, self.partial.name)
                return False
            with self._file:
                # A failed write can leave bytes beyond the last complete mdat.
                if self._file.tell() != self._good_size:
                    self._file.truncate(self._good_size)
                self._refresh_identity()
            return self._owns_partial() and self._finalize()
        finally:
            self._directories.close()

    def _finalize(self):
        try:
            return _publish(self.partial.name, self.path.name, self._date_fd, self._date_fd)
        except FileExistsError:
            pass
        # A file can appear after the partial was reserved. Keep the recording
        # by publishing it under the next available second, even across midnight.
        camera = self.path.parent.parent
        when = self._when
        while True:
            when += datetime.timedelta(seconds=1)
            path = segment_path(camera.parent, camera.name, when)
            with _directory(path.parent.name, self._camera_fd, create=True) as destination_fd:
                if _exists(destination_fd, path.name + ".part"):
                    continue
                try:
                    return _publish(self.partial.name, path.name, self._date_fd, destination_fd)
                except FileExistsError:
                    continue


class _Input:
    """Bounded read-ahead for one stream, including a warm-up stream during rotation."""

    def __init__(self, open_stream, camera_id, ready, clock):
        self.ready = ready
        self.chunks = deque()
        self.last_read = clock()
        self.done = self.closed = False
        self.stream = None
        self._buffer = bytearray()
        self.header = bytearray()
        threading.Thread(target=self._read, args=(open_stream, camera_id, clock),
                         name="backup-input-" + camera_id, daemon=True).start()

    def _read(self, open_stream, camera_id, clock):
        stream = None
        try:
            stream = open_stream(camera_id)
            read = getattr(stream, "read1", stream.read)
            with self.ready:
                self.stream = stream
            while True:
                with self.ready:
                    self.ready.wait_for(lambda: self.closed or len(self.chunks) < 4)
                    if self.closed:
                        break
                chunk = read(64 * 1024)
                if not chunk:
                    break
                with self.ready:
                    self.chunks.append(chunk)
                    self.last_read = clock()
                    self.ready.notify_all()
        except Exception:
            # Stream errors may carry upstream data. Treat them as EOF.
            pass
        finally:
            with self.ready:
                self.stream = None
                self.done = True
                self.ready.notify_all()
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass

    def close(self):
        with self.ready:
            self.closed = True
            stream = self.stream
            self.ready.notify_all()
        # A socket shutdown wakes a blocked HTTP read without taking its I/O lock.
        interrupt = getattr(stream, "interrupt", None)
        if interrupt is not None:
            try:
                interrupt()
            except OSError:
                pass

    def boxes(self, chunk):
        self._buffer.extend(chunk)
        offset = 0
        while True:
            size = _box_size(self._buffer, offset)
            if not size:
                break
            yield self._buffer[offset:offset + size]
            offset += size
        del self._buffer[:offset]


class _Worker:
    def __init__(self, service, camera_id, folder):
        self.id, self.folder = camera_id, folder
        self.state = "waiting"
        self.cancelled = threading.Event()
        self.storage_ready = threading.Event()
        self.ready = threading.Condition()
        self.thread = threading.Thread(target=service._work, args=(self,), name="backup-" + camera_id, daemon=True)

    def cancel(self):
        self.cancelled.set()
        self.storage_ready.set()
        with self.ready:
            self.ready.notify_all()


class BackupService:
    """The injected free-space callback takes no arguments. Wait takes a delay in seconds."""

    def __init__(self, store, open_stream, forbidden, clock=time.time, now=datetime.datetime.now,
                 free_space=None, segment_seconds=SEGMENT_SECONDS, stall_seconds=20, retry_seconds=5,
                 maintenance_seconds=300, wait=None):
        self.store, self.open_stream = store, open_stream
        self.forbidden = tuple(forbidden)
        # A clock correction may change filenames, but must not delay rotation.
        self.clock = time.monotonic if clock is time.time else clock
        self.now, self.free_space, self.wait = now, free_space, wait
        self.segment_seconds, self.stall_seconds = segment_seconds, stall_seconds
        self.retry_seconds, self.maintenance_seconds = retry_seconds, maintenance_seconds
        self._lock = threading.RLock()
        self._settings_lock = threading.Lock()
        self._file_lock = threading.Lock()
        self._wake = threading.Event()
        self._running = False
        self._thread = None
        self._workers = {}
        self._worker_threads = set()
        self._active = set()
        self._last = {}
        self._save_failures = {}
        self._folder = ""
        self._free = self._total = self._used = self._warning = None

    def status(self):
        with self._lock:
            state = self.store.snapshot()
            settings = state.get("backup", BACKUP_DEFAULTS)
            folder, policies = settings["folder"], settings["cameras"]
            current = folder == self._folder
            warning = self._warning if current else None
            cameras = []
            for camera in state["cameras"]:
                camera_id = camera["id"]
                days = policies.get(camera_id, 0)
                worker = self._workers.get(camera_id)
                recording = worker is not None and worker.folder == folder and not worker.cancelled.is_set()
                camera_state = (worker.state if recording else "waiting") if days else "off"
                if current and days and self._save_failures.get(camera_id, 0) >= 2:
                    camera_state = "save_failed"
                    warning = warning or "save_failed"
                cameras.append({"id": camera_id, "name": camera["name"], "days": days,
                                "state": camera_state,
                                "last": self._last.get(camera_id) if current and days else None})
            return {"folder": folder, "cameras": cameras,
                    "free_bytes": self._free if current else None, "total_bytes": self._total if current else None,
                    "used_bytes": self._used if current else None, "warning": warning}

    def configure(self, body):
        if not isinstance(body, dict) or set(body) != {"folder", "cameras"} or not valid_policies(
                body["cameras"], (0,) + DAYS):
            raise BackupError("invalid_settings")
        policies = {camera_id: days for camera_id, days in body["cameras"].items() if days}
        with self._settings_lock:
            ids = {camera["id"] for camera in self.store.snapshot()["cameras"]}
            if not body["cameras"].keys() <= ids:
                raise BackupError("invalid_settings")
            folder = validate_folder(body["folder"], self.forbidden)
            if not folder and policies:
                raise BackupError("invalid_settings")
            if folder:
                self._mark(folder)
            try:
                self.store.set_backup({"folder": folder, "cameras": policies})
            except ValueError:
                raise BackupError("invalid_settings") from None
            self.settings_changed()
            return self.status()

    @staticmethod
    def _mark(folder):
        try:
            with _directory(folder) as folder_fd:
                for name, content in (
                    (MARKER, ""),
                    ("README.txt", "The Kasa dashboard manages recordings in this folder.\n"
                                   "It removes old recordings automatically and frees space when the drive is low.\n"),
                ):
                    try:
                        fd = os.open(name, CREATE_FLAGS, 0o600, dir_fd=folder_fd)
                    except FileExistsError:
                        if name == MARKER and _regular_file(folder_fd, name) is None:
                            raise BackupError("folder_readonly")
                    else:
                        with os.fdopen(fd, "w", encoding="utf-8") as file:
                            file.write(content)
        except OSError:
            raise BackupError("folder_readonly") from None

    def settings_changed(self):
        deadline = time.monotonic() + STOP_SECONDS
        with self._lock:
            retiring = self._sync_workers()
            self._wake.set()
        self._join_threads(retiring, deadline)

    def _sync_workers(self):
        self._worker_threads = {thread for thread in self._worker_threads if thread.is_alive()}
        settings = self.store.get_backup()
        folder, cameras = settings["folder"], settings["cameras"]
        if folder != self._folder:
            self._folder = folder
            self._free = self._total = self._used = self._warning = None
            self._last.clear()
            self._save_failures.clear()
        retiring = []
        for camera_id, worker in self._workers.items():
            if not self._running or worker.folder != folder or camera_id not in cameras:
                worker.cancel()
            if worker.cancelled.is_set():
                retiring.append(worker.thread)
        if self._running:
            for camera_id in cameras:
                if camera_id not in self._workers:
                    worker = self._workers[camera_id] = _Worker(self, camera_id, folder)
                    worker.thread.start()
                    self._worker_threads.add(worker.thread)
        return retiring

    def start(self):
        with self._lock:
            if (self._running or (self._thread is not None and self._thread.is_alive()) or
                    any(thread.is_alive() for thread in self._worker_threads)):
                return
            self._running = True
            self._sync_workers()
            self._wake.set()
            self._thread = threading.Thread(target=self._maintain, name="backup-maintenance", daemon=True)
            self._thread.start()

    def stop(self):
        deadline = time.monotonic() + STOP_SECONDS
        with self._lock:
            self._running = False
            for worker in self._workers.values():
                worker.cancel()
            self._wake.set()
            # A worker may already have released its camera slot while it exits.
            threads = [*self._worker_threads, self._thread]
        self._join_threads(threads, deadline)

    @staticmethod
    def _join_threads(threads, deadline):
        for thread in threads:
            if thread is not None and thread is not threading.current_thread():
                thread.join(max(0, deadline - time.monotonic()))

    @contextmanager
    def _root(self, folder):
        try:
            root = Path(validate_folder(folder, self.forbidden))
            root_fd = os.open(root, DIRECTORY_FLAGS)
        except (OSError, BackupError):
            raise BackupError("folder_missing") from None
        try:
            if _regular_file(root_fd, MARKER) is None:
                raise BackupError("folder_missing")
            yield root, root_fd
        finally:
            os.close(root_fd)

    def _space(self, root):
        usage = shutil.disk_usage(root)
        free = self.free_space() if self.free_space is not None else usage.free
        return free, usage.total

    def _storage_status(self, folder, free, total, warning, used=None):
        with self._lock:
            if self._folder == folder:
                changed = self._warning != warning
                self._free, self._total, self._warning = free, total, warning
                if used is not None or warning == "folder_missing":
                    self._used = used
                if changed:
                    for worker in self._workers.values():
                        if worker.folder == folder:
                            if warning is None:
                                worker.storage_ready.set()
                            else:
                                with worker.ready:
                                    worker.ready.notify_all()

    def _check_storage(self, folder):
        try:
            with self._root(folder) as (root, _):
                free, total = self._space(root)
        except (OSError, BackupError):
            self._storage_status(folder, None, None, "folder_missing")
            raise BackupError("folder_missing") from None
        warning = "low_space" if free < MIN_FREE_BYTES else None
        self._storage_status(folder, free, total, warning)
        if warning:
            self._wake.set()
            raise BackupError(warning)

    def _maintain(self):
        while True:
            self._wake.wait(self.maintenance_seconds)
            self._wake.clear()
            with self._lock:
                if not self._running:
                    return
                self._sync_workers()
            self._maintenance_pass()

    def _maintenance_pass(self):
        with self._settings_lock:
            settings = self.store.get_backup()
            folder, cameras = settings["folder"], settings["cameras"]
            if not folder:
                return
            try:
                with self._root(folder) as (root, root_fd):
                    _, used = _prune(root_fd, root, cameras, self.now(), lambda: self._space(root)[0],
                                     MIN_FREE_BYTES, self._active, self._prune_guard)
                    with os.scandir(root_fd) as entries:
                        disabled = [entry.name for entry in entries
                                    if entry.name not in cameras and CAMERA_ID.fullmatch(entry.name)]
                    used += sum(info.st_size for _, camera_fd, day in _dates(root_fd, disabled)
                                for _, _, partial, info in _scan_date(camera_fd, day) if not partial)
                    free, total = self._space(root)
            except Exception:
                self._storage_status(folder, None, None, "folder_missing")
                return
            self._storage_status(folder, free, total, "low_space" if free < MIN_FREE_BYTES else None, used)

    @contextmanager
    def _prune_guard(self, camera_id):
        with self._file_lock, self.store.lock:
            yield camera_id in self.store.get_backup()["cameras"]

    def _work(self, worker):
        delay = self.retry_seconds
        try:
            while not worker.cancelled.is_set():
                if not self._wanted(worker):
                    break
                worker.state = "waiting"
                worker.storage_ready.clear()
                try:
                    self._check_storage(worker.folder)
                    if not worker.cancelled.is_set():
                        self._record(worker)
                except BackupError as error:
                    if error.code in ("folder_missing", "low_space"):
                        worker.state = "no_folder" if error.code == "folder_missing" else "low_space"
                        if self._pause(worker, STORAGE_RETRY_SECONDS, worker.storage_ready):
                            break
                        continue
                except Exception:
                    pass
                if worker.state == "recording":
                    delay = self.retry_seconds
                worker.state = "waiting"
                if self._pause(worker, delay):
                    break
                delay = min(60, delay * 2)
        finally:
            with self._lock:
                del self._workers[worker.id]
                if worker.cancelled.is_set():
                    self._sync_workers()

    def _wanted(self, worker):
        settings = self.store.get_backup()
        return settings["folder"] == worker.folder and worker.id in settings["cameras"]

    def _pause(self, worker, delay, wake=None):
        if worker.cancelled.is_set():
            return True
        if self.wait is None:
            (wake if wake is not None else worker.cancelled).wait(delay)
            return worker.cancelled.is_set()
        return self.wait(delay) or worker.cancelled.is_set()

    def _finish(self, worker, segment):
        if segment is None:
            return
        try:
            saved = segment.finish()
        except Exception:
            # Leave any on-disk partial for maintenance and continue recording.
            saved = False
        finally:
            with self._file_lock:
                self._active.discard(segment.partial)
        with self._lock:
            if worker.folder != self._folder:
                return
            if saved:
                self._last[worker.id] = self.now().isoformat()
                self._save_failures.pop(worker.id, None)
            elif segment.has_video:
                self._save_failures[worker.id] = self._save_failures.get(worker.id, 0) + 1

    def _record(self, worker):
        current = _Input(self.open_stream, worker.id, worker.ready, self.clock)
        upcoming, segment, rotate_at = None, None, None
        try:
            while not worker.cancelled.is_set() and self._wanted(worker):
                with self._lock:
                    warning = self._warning if worker.folder == self._folder else None
                if warning in ("low_space", "folder_missing"):
                    raise BackupError(warning)
                if rotate_at is not None and self.clock() >= rotate_at and upcoming is None:
                    self._check_storage(worker.folder)
                    upcoming = _Input(self.open_stream, worker.id, worker.ready, self.clock)
                with worker.ready:
                    if worker.cancelled.is_set():
                        return
                    sources = (upcoming, current) if upcoming is not None else (current,)
                    # Once a replacement is opening, let it finish warming up
                    # even if the old stream ends in the meantime.
                    target = sources[0]
                    deadline = target.last_read + self.stall_seconds
                    if not target.chunks and (target.done or self.clock() >= deadline):
                        return
                    source = next((item for item in sources if item.chunks), None)
                    if source is None:
                        if rotate_at is not None and upcoming is None:
                            deadline = min(deadline, rotate_at)
                        worker.ready.wait(max(0, deadline - self.clock()))
                        continue
                    chunk = source.chunks.popleft()
                    worker.ready.notify_all()
                for box in source.boxes(chunk):
                    kind = box[4:8]
                    if (source is upcoming or segment is None) and kind != b"moof":
                        if kind == b"mdat" or len(source.header) + len(box) > 2 * MAX_BOX_BYTES:
                            raise BackupError("stream")
                        source.header.extend(box)
                        continue
                    if source is upcoming or segment is None:
                        if source is upcoming:
                            current.close()
                            current, upcoming = upcoming, None
                        self._finish(worker, segment)
                        segment = None
                        with self._root(worker.folder) as (root, root_fd), self._file_lock:
                            segment = _Segment(root, worker.id, self.now(), root_fd, source.header)
                            self._active.add(segment.partial)
                        source.header = bytearray()
                        rotate_at = self.clock() + self.segment_seconds
                    if segment.box(box):
                        worker.state = "recording"
        finally:
            self._finish(worker, segment)
            current.close()
            if upcoming is not None:
                upcoming.close()
