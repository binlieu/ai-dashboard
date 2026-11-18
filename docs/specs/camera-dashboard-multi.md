# Codex task spec — multiple cameras (follow-up to camera-dashboard)

Effort: high

## Goal
Let the dashboard show any number of Kasa cameras (two today) in a grid, configured by a list of
MAC addresses in `.env`, all using the same Kasa login.

## Scope — touch ONLY these
- `start.sh`, `scripts/lib.sh`, `tests/test-lib.sh`
- `go2rtc.yaml` — **delete it** (replaced by a generated file, see below)
- `www/index.html`, `www/dashboard.css`, `www/dashboard.js`
- `.env.example`, `README.md`, `.gitignore`

Do NOT modify `www/vendor/*`, `scripts/setup.sh`, `.env`. Do NOT read or print `.env`.

## What to do

### 1. Configuration (`.env`)
- New: `CAMERA_MACS` — comma-separated MAC list (spaces around commas allowed).
- New, optional: `CAMERA_NAMES` — comma-separated display names, positional. Missing or empty
  entries default to `Camera N` (N = 1-based position).
- Backward compatible: if `CAMERA_MACS` is absent, fall back to the old single `CAMERA_MAC`
  (and the old `CAMERA_IP` override, which then applies to that one camera only). If
  `CAMERA_MACS` is set, ignore `CAMERA_MAC`/`CAMERA_IP`.
- Optional `CAMERA_IPS` — comma-separated, positional IP overrides; an empty entry means
  "discover this one by MAC".
- Reject duplicates and invalid MACs with a message naming the bad entry.

### 2. `scripts/lib.sh` — add pure helpers (and tests for each)
- `split_csv STRING` — print one trimmed item per line, preserving empty items
  (`"a, ,b"` → `a`, empty, `b`). bash 3.2 compatible.
- `render_go2rtc_config PORT CAMERA_LINES` and `render_cameras_json CAMERA_LINES` — pure functions
  that print the generated file contents to stdout; `start.sh` only redirects them to the files.
  `CAMERA_LINES` is one camera per line, tab-separated: `index<TAB>name<TAB>ip` with an empty ip
  for a skipped camera. Build the config with single-quoted `printf` formats (or a quoted
  heredoc), **never an unquoted heredoc**: `KASA_USER_ENC` and `KASA_PASS_B64` are exported in
  `start.sh`, so an unquoted heredoc would silently write the real login into the file.
  Required tests: with `KASA_PASS_B64=SENTINEL_SECRET` and `KASA_USER_ENC=SENTINEL_USER`
  exported, the rendered config contains the literal text `${KASA_PASS_B64}` and
  `${KASA_USER_ENC}` and does not contain either sentinel; with camera 2 of 3 skipped the config
  has streams `cam1` and `cam3` only and the JSON lists all three with `cam2` unavailable; the
  JSON contains no IP or MAC; the JSON parses (`python3 -c 'import json,sys; json.load(sys.stdin)'`).
- `json_string STRING` — print a JSON string literal including the quotes, escaping `"`, `\`,
  control characters, and `<`, `>`, `&` as `\u00XX` (names end up in a file the page loads).
  `python3` is fine; pass the value on stdin, not argv.

### 3. `start.sh`
1. Build the camera list (index i = 1..N): name, MAC, optional IP override.
2. Resolve each camera's IP via `ip_for_mac`. If any are unresolved, do **one** ping sweep (the
   existing one) and retry only the unresolved ones.
3. A camera that still can't be found is **skipped with a warning** on stderr
   (`Camera "<name>" (<mac>) not found on this network; skipping.`). Exit 1 only if none resolve.
4. Generate the go2rtc config at `run/go2rtc.yaml` (create `run/`, mode 700; file mode 600),
   overwriting each start. Same settings as the deleted `go2rtc.yaml`, keeping
   `static_dir: "www"`: go2rtc resolves it relative to its **working directory**, not the config
   file (verified), and it is started with `exec bin/go2rtc -c run/go2rtc.yaml` from the repo root.
   One stream per **resolved** camera, id `cam<i>` (i = position in the configured list, so ids
   are stable when a camera is skipped):
   `cam<i>: kasa://${KASA_USER_ENC}:${KASA_PASS_B64}@<literal ip>:19443/https/stream/mixed?video=h264&audio=g711&resolution=hd`
   Keep the `${KASA_USER_ENC}` / `${KASA_PASS_B64}` placeholders literal in the file (go2rtc
   substitutes them from the environment) — the generated file must contain no credentials.
5. Generate `www/cameras.json` (mode 644): an array in configured order of
   `{"id":"cam1","name":"Front","available":true}`; skipped cameras get `"available":false`.
   Names via `json_string`. No MACs, IPs or credentials in this file.
6. Print the dashboard URL and one line per camera: name, IP or "not found". Never credentials.
7. Everything else (credential handling, port, `--open`, setup) stays as is.

### 4. Dashboard page
- On load, `fetch('cameras.json', {cache: 'no-store'})`; if it fails or is empty, show a clear
  message ("No cameras configured. Check .env and restart start.sh.").
- Render one tile per camera in a responsive grid: 1 camera → the current single large layout;
  2 → two columns on wide screens, stacked under ~900 px; 3–4 → 2×2; more → auto-fill.
  The whole grid should fit the viewport without scrolling for up to 4 cameras on a 16:9
  monitor (this will run full-screen on a wall display).
- Each tile: the camera's name (replacing the hard-coded "CAM 01" label; set via `textContent`,
  never `innerHTML`), its own status pill and "last frame" text, its own Snapshot and Fullscreen
  buttons. Snapshot filename: `<slug-of-name>-YYYYMMDD-HHMMSS.jpg`.
- Refactor the existing single-player logic into a per-camera unit (class or factory) so each
  tile has independent stall tracking and forced-reconnect state. Keep the exact behaviours from
  the previous spec: status driven by playback progress, hidden-tab pause, forced reconnect via
  `ondisconnect()` then `onconnect()` after the old socket's close, at most once per 30 s.
- Stream URL per tile: `api/ws?src=<id>`; snapshot `api/frame.jpeg?src=<id>` (encode the id).
- Unavailable cameras (`available:false`) render a tile with the name and a static
  "Not found on the network" state — no player, no requests.
- Header keeps the title and clock; replace the single global pill with a short summary such as
  "2 of 2 live".
- Still no requests other than `cameras.json`, `api/ws` and `api/frame.jpeg`. Never `api/streams`
  or `api/config`.

### 5. `.gitignore`, `.env.example`, `README.md`
- `.gitignore`: add `run/` and `www/cameras.json`.
- `.env.example`: document `CAMERA_MACS`, `CAMERA_NAMES`, `# CAMERA_IPS=`; note that the old
  `CAMERA_MAC` still works for one camera.
- `README.md`: update setup, "how it works" (generated files), and the local-checks grep (it
  referenced `go2rtc.yaml`; point it at `www` and `run/` instead).

## Constraints
- bash 3.2 compatible; no new dependencies; credentials never in stdout/stderr, generated files,
  `www/`, or any command line; all listeners on `127.0.0.1`.
- A go2rtc instance from the previous version may be running from this worktree on port 1984.
  Do not stop it, start another, or contact the cameras. The reviewer does the live check.
- Do not commit; leave changes in the working tree.

## Done when
- [ ] `/bin/bash tests/test-lib.sh` passes, including new cases for `split_csv`, `json_string`
      (quotes, backslash, `<script>`, a non-ASCII name), and the existing 41.
- [ ] `/bin/bash -n start.sh scripts/lib.sh` is clean.
- [ ] `grep -rn "KASA_PASSWORD\|KASA_EMAIL\|api/streams\|api/config" www` finds nothing.
- [ ] Final message lists files changed and what you could not verify.

## Context Codex needs
- Second camera for testing: MAC `AA:BB:CC:DD:EE:02`, currently `192.168.1.147`; first camera
  `AA:BB:CC:DD:EE:01` at `192.168.1.178`. Both verified to answer the same Kasa login on :19443.
- Each camera accepts only **one** stream connection at a time (a second gets 503); go2rtc holds
  that one connection and fans it out, so several tiles/tabs of the same camera are fine.
- The first camera currently delivers only ~3–5 fps over a weak link; the page must treat slow
  but advancing playback as Live (the existing 5 s progress window already does).
