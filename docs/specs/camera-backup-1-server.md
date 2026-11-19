# Codex task spec — Camera backup 1/2: recorder, clean-up, settings and API

Effort: xhigh

## Goal
Selected cameras are recorded around the clock as 3-minute MP4 files into a folder the user
chose, and each camera's files are kept for 1, 7 or 30 days. Old files are removed
automatically, and earlier when the drive runs low on space.

## Scope — touch ONLY these
- `app/backup.py` (new), `app/security.py`, `app/store.py`, `app/server.py`, `app/__main__.py`
- `app/go2rtc.py` (only to add a method that opens the MP4 stream over the private socket)
- `tests/test_backup.py` (new), `tests/test_store.py`, `tests/test_server.py`, `tests/test_go2rtc.py`
- `README.md` (a "Backup" section and the API table)

No page files in this task.

## Acceptance tests (written by Claude — do NOT modify)
- `tests/test_backup_acceptance.py`, every other `tests/*_acceptance.py`, `tests/test_security.py`.
  All must pass: `/usr/bin/python3 -m unittest discover -s tests -t .`
  If one looks wrong, stop and say why in your final message instead of changing it.

## What to do

### Pure helpers in `app/backup.py` (fixed by the acceptance tests)
- `DAYS = (1, 7, 30)`, `SEGMENT_SECONDS = 180`, `MARKER = ".kasa-dashboard-backup"`,
  `MIN_FREE_BYTES = 5 * 1024**3`, `BackupError(code)` with a fixed sentence per code (never
  echo paths or stream bytes in `str()`).
- `validate_folder(text, forbidden=())` → the resolved absolute path as `str` (`""` stays `""`
  and means "backup off"). Codes: `invalid_folder` (not a string, not absolute, `~`, over 1024
  characters, control characters, or the filesystem root), `folder_missing` (not an existing
  directory — **never create it**), `folder_forbidden` (after resolving symlinks it is inside,
  equal to, or a parent of any `forbidden` directory), `folder_readonly` (no write+execute access).
- `segment_path(root, camera_id, when)` → `Path(root)/camN/YYYY-MM-DD/HH-MM-SS.mp4`; only ids
  matching `CAMERA_ID`.
- `split_boxes(data)` → how many leading bytes of `data` are whole top-level MP4 boxes.
  Box = 4-byte big-endian size + 4-byte type; size 1 means a 64-bit size follows the type.
  Refuse (`BackupError("stream")`): size 0, size below the header length, a box over 64 MiB, or
  a type that is not four printable ASCII characters. An incomplete trailing box is not an
  error; it is simply not counted.
- `prune(root, policies, now, free_space, min_free, active=())` → list of removed `Path`s in the
  order removed. **This deletes user files; follow the rules exactly.**
  1. Do nothing (return `[]`) unless `root` is an existing directory containing a regular file
     named `MARKER`, and `policies` is a dict of `CAMERA_ID` → a value in `DAYS` (plain `int`,
     not `bool`); any invalid entry makes the whole call a no-op.
  2. A *segment* is a regular file (never a symlink) at exactly
     `root/<camera id>/<YYYY-MM-DD>/<HH-MM-SS>.mp4` where the camera id is a key of `policies`,
     the camera directory and the date directory are real directories (not symlinks), and the
     date and time are valid. Its time comes from the names, never from file metadata.
     A *partial* is the same with `.mp4.part`.
  3. Remove every segment whose time is earlier than `now - days` for its camera.
  4. Remove every partial older than one hour that is not in `active`.
  5. Then, while there are removable segments: call `free_space()` once; if it is at least
     `min_free`, stop; otherwise remove the oldest remaining segment across all policy cameras
     (ties: lower path first) and repeat. Paths in `active` are never removed.
  6. Finally remove date directories (matching the pattern, not symlinks) that are now empty.
     Never remove a camera directory, the marker, the root, or anything else.
  7. Use `os.scandir`/`lstat`-style checks that do not follow symlinks; a failure to delete one
     file is skipped, not raised.

### Settings (`app/store.py`)
- Optional top-level `backup`: `{"folder": str, "cameras": {camera id: days}}`. Defaults
  `{"folder": "", "cameras": {}}`. `folder` is `""` or an absolute path without control
  characters, at most 1024 characters (existence is not checked in the store). Camera keys must
  be existing camera ids, values in `DAYS`; a non-empty `cameras` needs a folder. Exactly these
  keys. `get_backup()`, `set_backup(backup)` (returns the saved value; invalid → `ValueError`,
  nothing changes). Removing a camera drops its entry. Files without `backup` load unchanged
  and are not rewritten; invalid `backup` on load resets backup only (one warning). Camera
  rollback keeps a backup save made meanwhile (same pattern as weather and devices).

### Recorder (`BackupService` in `app/backup.py`)
- `BackupService(store, open_stream, forbidden, clock=time.time, now=datetime.datetime.now,
  free_space=None)`; `open_stream(camera_id)` returns a readable binary stream of
  `GET /api/stream.mp4?src=<id>` from go2rtc (add `Go2RTC.open_mp4(camera_id)` using the existing
  Unix-socket HTTP connection; status must be 200, else raise; chunked decoding handled by
  `http.client`; the caller closes it). The browser-facing gate still refuses `stream.mp4`.
- One worker thread per camera with backup on. Loop: open a stream; create
  `segment_path(...)` with `.part` appended (create the camera and date directories, mode 700,
  files 600; the root itself is never created); copy the stream, writing only whole top-level
  boxes (`split_boxes`); after `SEGMENT_SECONDS` open the **next** stream first and switch files
  once it has delivered its first whole box, so the gap between files is as small as possible;
  on switching or stopping, close the file and rename `.part` → `.mp4` (a file with no `moof`
  box is deleted instead). A stream that ends, stalls for 20 s, or fails → close what was
  written the same way, wait (5 s, doubling to 60 s), reconnect. Never let one camera's failure
  affect another or the dashboard.
- Before each segment: the root must exist, be a directory, and contain `MARKER` (the marker is
  written when settings are saved, see below). If the root is missing (drive unplugged) the
  camera waits and retries every 30 s, writing nothing anywhere.
- A maintenance thread runs `prune` every 5 minutes and right after settings change, with
  `policies` from the store, `active` = the files currently being written, and
  `free_space` = `shutil.disk_usage(root).free` by default.
- When free space is below `MIN_FREE_BYTES` and nothing more can be removed, workers pause
  writing and retry every 30 s.
- `status()` (never touches the network or walks the disk; cached by the workers/maintenance):
  `{"folder": str, "cameras": [{"id", "name", "days": 0|1|7|30, "state", "last": ISO time or None}],
  "free_bytes": int|None, "total_bytes": int|None, "used_bytes": int|None, "warning": code|None}`
  with `state` in `off`, `recording`, `waiting` (no stream yet / reconnecting), `no_folder`,
  `low_space`; `warning` in `folder_missing`, `low_space`, or `None`. `used_bytes` is the size
  of this dashboard's segments, refreshed by the maintenance pass.
- `configure(body)`: body exactly `{"folder": str, "cameras": {id: 0|1|7|30}}` (0 = off);
  `validate_folder(folder, forbidden)`; write `MARKER` in the root if absent (plus a short
  `README.txt` once, saying the folder is managed by the dashboard and files are removed
  automatically); save to the store; start/stop workers to match; return `status()`.
  Changing the folder never moves or deletes files in the old folder.
- `start()` / `stop()`; `stop()` finalises open files within 5 s.
- `forbidden` = the project directory.

### Server
- Gate: `GET, PUT /api/backup` (same rules as `/api/weather`).
- Routes call `status()` / `configure()`; `BackupError` → 400 with `{error, message}` (use the
  codes above; messages are fixed sentences such as "That folder does not exist. Check that the
  drive is connected."). Wire the service in `__main__` (start after go2rtc is ready, stop on
  shutdown). `DashboardApplication` takes `backup=None`; tests inject a fake.
- Camera removal stops that camera's worker (its files stay until they age out is NOT required:
  files of a camera no longer in `policies` are simply left alone forever).

### README
A "Backup" section: what is recorded (video only, no sound, 3-minute MP4 files), the folder
layout, retention and low-space behaviour, rough sizes (about 8–15 GB per camera per day),
that recording keeps cameras streaming continuously, that the folder is never created
automatically and nothing outside the dashboard's own files is ever deleted, and the API rows.

## Constraints
- Python 3.9 standard library only. Match existing style.
- Tests use temp directories and fake streams only; never the real go2rtc, never real camera
  video, never paths outside the temp directory. Worker timing must be injectable so tests do
  not sleep for real (keep the whole suite's added time under 5 s).
- Never log folder paths' contents, video bytes, or credentials.
- A dashboard is running from the main checkout on port 1984: do not stop it, start another on
  that port, or send it write requests.
- Public repository: placeholder data only. Do not commit.

## Done when
- [ ] `/usr/bin/python3 -m unittest discover -s tests -t .` passes completely.
- [ ] `git diff --stat HEAD -- tests/test_security.py 'tests/*_acceptance.py' www` is empty.
- [ ] Final message lists files changed and what you could not verify.
