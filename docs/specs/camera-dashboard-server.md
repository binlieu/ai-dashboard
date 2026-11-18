# Codex task spec — Python front server with add-by-MAC API (job 1 of 2)

Effort: high

## Goal
Replace the bash launcher logic with a small Python (stdlib-only) server that is the **only**
network listener, hides go2rtc behind a private Unix socket, and exposes a safe same-origin API
to add / rename / remove cameras by MAC address and to store the grid layout.

## Why (read this first)
go2rtc's HTTP API has no cross-site protection for writes: on a throwaway instance a request
shaped like a cross-site form POST (`POST /api/config`, `Content-Type: text/plain`, foreign
`Origin`) rewrote its config file (an `exec:` stream can be injected that way) and
`POST /api/exit` shut it down. Today go2rtc listens on `127.0.0.1:1984`, so any web page open
in the user's browser could do that. After this job go2rtc must have **no TCP/UDP listener**.

## Scope — touch ONLY these
- New: `app/__init__.py`, `app/__main__.py`, `app/security.py`, `app/discovery.py`,
  `app/store.py`, `app/go2rtc.py`, `app/server.py`, `app/envfile.py`
- New tests: `tests/__init__.py`, `tests/test_discovery.py`, `tests/test_store.py`,
  `tests/test_go2rtc.py`, `tests/test_envfile.py`, `tests/test_server.py`
- Change: `start.sh` (becomes thin), `www/dashboard.js` (one small change, see §7),
  `.gitignore`, `.env.example`, `README.md`
- Delete: `scripts/lib.sh`, `tests/test-lib.sh` (their behaviour moves to Python with tests)

Do NOT modify: `tests/test_security.py`, `www/vendor/*`, `scripts/setup.sh`, `www/index.html`,
`www/dashboard.css`, `.env`. Do NOT read or print `.env`.

## Acceptance tests (written by Claude — do NOT modify)
- `tests/test_security.py` — `/usr/bin/python3 -m unittest tests.test_security -v`; all must pass.
  Do not edit, skip, or weaken these tests; if one looks wrong, stop and say why in your final
  message instead of changing it. Its docstring is the contract for `app/security.py`.

## What to do

### 1. `app/security.py` — `check_request(method, target, headers, port) -> int`
Pure function, no I/O; the single gate every request passes before anything else happens.
Rules (the acceptance tests are authoritative where this list is silent):
- `Host` must be exactly `127.0.0.1:<port>` or `localhost:<port>` → else 403.
- Parse the target strictly. Reject (403/404) anything that is not an origin-form path, has
  `%`-encoded slashes/dots/backslashes/NUL, `..` segments, `//`, a backslash, a dotfile segment,
  or is longer than 2048 chars. Never raise.
- "Same origin" means the `Origin` header equals `http://` + the request's own `Host` value,
  byte for byte (so `Origin: http://localhost:1984` with `Host: 127.0.0.1:1984` is foreign).
- Cross-site signal: an `Origin` header that is present and not same-origin, or a
  `Sec-Fetch-Site` header present with a value other than `same-origin` or `none`.
  Any `/api/...` request with a cross-site signal → 403.
- Route table (anything else → 404; right path, wrong method → 405 or 404):
  - `GET|HEAD` static: `/` and files; no query string restrictions needed beyond the parse rules.
  - `GET /api/cameras` — no query string allowed.
  - `GET /api/frame.jpeg?src=cam<digits>` — exactly one query parameter, `src`, matching
    `^cam[1-9][0-9]{0,3}$`.
  - `GET /api/ws?src=cam<digits>` — same query rule; must be a WebSocket upgrade
    (`Upgrade: websocket`, `Connection` containing `upgrade`, case-insensitive) and must carry a
    same-origin `Origin` (missing → 403).
  - `POST /api/cameras`, `PATCH /api/cameras/<id>`, `DELETE /api/cameras/<id>`, `PUT /api/layout`
    — `<id>` matches the same `cam` pattern; no query string; a same-origin `Origin` is
    **required** (missing → 403); POST/PATCH/PUT also require `Content-Type` whose media type is
    exactly `application/json` (parameters like `; charset=utf-8` allowed) → else 415. Check
    Origin before Content-Type.

### 2. `app/envfile.py`, `app/discovery.py` (ports of `scripts/lib.sh`, same semantics)
- `env_get(path, key)` — same parsing rules as the bash version (no shell evaluation, first
  match, strip CR and one pair of matching quotes). Returns `None` when missing.
- `normalize_mac(text)` → canonical `aa:bb:cc:dd:ee:ff` (lower-case, **zero-padded**; accept `:`
  or `-`, and 1–2 hex digits per octet as macOS `arp` prints `c:70:43:ad:c0:b6`); invalid → `None`.
- `parse_arp(text)` → dict canonical-mac → ip, skipping `(incomplete)` rows.
  `ip_for_mac(mac, arp_text=None)` runs `/usr/sbin/arp -an` when text is not given.
- `sweep_local_network()` — one parallel ping sweep of the default route's /24 (the existing
  logic: `route -n get default`, `ipconfig getifaddr`, `ping -c 1 -t 1`), bounded to ~5 s total.
  Use `subprocess` with argument lists, never a shell string.
- Port the existing bash test cases to `tests/test_discovery.py` / `tests/test_envfile.py`.

### 3. `app/store.py` — persistent state in `data/cameras.json` (gitignored)
- Shape: `{"version":1,"next_id":3,"layout":"auto","cameras":[{"id":"cam1","name":"Camera 1","mac":"aa:bb:cc:dd:ee:01"}]}`.
- Ids are `cam<N>`, allocated from `next_id`, never reused. Max 16 cameras. Name: trimmed,
  1–40 chars, no control characters; default `Camera <N>`. Layout ∈ `auto`, `2x1`, `2x2`,
  `3x3`, `4x4`.
- Atomic save (write temp file in the same dir, `os.replace`), file mode 600, dir mode 700.
  A corrupt file → rename it to `cameras.json.corrupt-<timestamp>` and start empty with a warning.
- First run only (file absent): seed from `.env` `CAMERA_MACS` / `CAMERA_NAMES` (fall back to the
  single `CAMERA_MAC`), same validation as now. After that `.env` camera keys are ignored.
- Thread-safe (one lock around every mutation + save).

### 4. `app/go2rtc.py`
- `render_config(socket_path, cameras_with_ips)` → YAML text:
  ```yaml
  api:
    listen: ""
    unix_listen: "<socket_path>"
  rtsp:
    listen: ""
  webrtc:
    listen: ""
  streams:
    cam1: kasa://${KASA_USER_ENC}:${KASA_PASS_B64}@<ip>:19443/https/stream/mixed?video=h264&audio=g711&resolution=hd
  log:
    level: info
  ```
  The `${...}` placeholders stay literal in the file (test this with the env vars set to
  sentinels, as the bash test did). No `static_dir` — Python serves the files now. Cameras
  without an IP are omitted. Written to `run/go2rtc.yaml`, mode 600.
- Socket path: `<repo>/run/api.sock` if its absolute path is ≤ 100 bytes; otherwise a fresh
  `tempfile.mkdtemp(prefix="kcd-", dir="/tmp")` (mode 700) + `/api.sock`. (go2rtc fails with
  `bind: invalid argument` on longer paths — macOS limit is 104.) Remove a stale socket file
  before starting go2rtc; remove socket (and temp dir) on exit.
- Process supervision: spawn `bin/go2rtc -c run/go2rtc.yaml` with cwd = repo root and the two
  credential env vars; wait until the socket answers `GET /api/streams` (≤ 10 s); if the child
  exits, restart with backoff (1 s, 2 s, 5 s, then every 10 s); terminate it on shutdown.
  Forward go2rtc's output to our stdout with any `kasa://...@` userinfo masked as `kasa://***@`.
- Unix-socket HTTP client (`http.client.HTTPConnection` subclass overriding `connect`):
  `frame(id)` → `GET /api/frame.jpeg?src=<id>` (15 s timeout) and `ready()` →
  `GET /api/streams` returns 200. **Do not call go2rtc's `PUT`/`PATCH`/`DELETE /api/streams`
  at all**: verified here, `PUT /api/streams` rewrites the config file with the full source URL,
  which would write the real login to disk.
- `apply(cameras_with_ips)` — the only way streams change: re-render `run/go2rtc.yaml` from the
  store (placeholders literal), then restart the child and wait for `ready()` (≤ 10 s). All
  video drops for about two seconds; the page's player reconnects on its own. The config is
  re-rendered from the store before **every** spawn, including crash restarts, so the file on
  disk can never be stale or carry anything go2rtc wrote into it.
- `probe_camera(ip, user, password_b64)` → `GET https://<ip>:19443/https/stream/mixed?video=h264&audio=g711&resolution=hd`
  with Basic auth `email:base64(password)`, certificate verification off (self-signed device
  cert on a LAN address the user chose), 6 s timeout, read headers only then close.
  Returns one of `ok` (200), `busy` (503 — the camera serves one connection at a time and
  go2rtc may already hold it), `auth` (401), `unreachable` (anything else / connection error).
  macOS system Python 3.9 uses LibreSSL 2.8.3; the camera's TLS is old — if the default context
  fails the handshake, lower the minimum version / cipher level for this one connection only.

### 5. `app/server.py` + `app/__main__.py`
- `ThreadingHTTPServer` bound to `127.0.0.1:<DASHBOARD_PORT>` only. HTTP/1.1.
  Every request: build lower-case header dict → `check_request` → non-200 means send that
  status with a short plain-text body and nothing else.
- Response headers on everything: `X-Content-Type-Options: nosniff`, `Referrer-Policy:
  no-referrer`, `Cache-Control: no-store`, `Cross-Origin-Resource-Policy: same-origin`,
  `Content-Security-Policy: default-src 'self'; connect-src 'self' ws://127.0.0.1:<port>
  ws://localhost:<port>; img-src 'self' blob:; media-src 'self' blob:; style-src 'self'
  'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'`.
  Never send any `Access-Control-*` header.
- Static files from `www/` only: resolve with `os.path.realpath` and require the result to stay
  inside `realpath(www)`; fixed content-type map (`.html .js .css .json .svg .png .ico`);
  `/` → `index.html`; anything else 404.
- `GET /api/cameras` → `{"layout":"auto","max":16,"cameras":[{"id","name","mac","ip","available"}]}`
  (`ip` null and `available` false when unresolved).
- `POST /api/cameras` body `{"mac":"..","name":".."}` (name optional; body ≤ 4 KB; must be a
  JSON object). Steps, each failure returning `{"error":"<code>","message":"<human text>"}`:
  invalid MAC → 400 `invalid_mac`; duplicate → 409 `duplicate`; limit → 409 `limit`;
  not in ARP → one sweep → still missing → 404 `not_found`; probe `auth` → 422 `auth`;
  probe `unreachable` → 422 `not_kasa`; else save to store, `apply`, return 201 with the
  camera object. If `apply` fails (go2rtc not ready in time), roll the store back, `apply`
  again with the previous list, and return 502 `go2rtc`.
  Run the probe **before** any restart; `busy` cannot happen for a camera go2rtc doesn't hold
  yet, but treat it as success anyway.
- `PATCH /api/cameras/<id>` body `{"name":".."}` → 200 camera; unknown id → 404.
- `DELETE /api/cameras/<id>` → delete from store, `apply` → 204; unknown id → 404.
  (`PATCH` name changes do not touch go2rtc.)
- `PUT /api/layout` body `{"layout":"2x2"}` → 200 `{"layout":"2x2"}`; invalid → 400.
- `GET /api/frame.jpeg?src=<id>` → unknown id 404; else relay go2rtc's status, `Content-Type`
  and body (cap 10 MB).
- `GET /api/ws?src=<id>` → unknown id 404; else open the Unix socket, send a fresh upgrade
  request (`GET /api/ws?src=<id>`, `Host: localhost`, `Origin: http://localhost`, plus only the
  client's `Sec-WebSocket-Key`, `-Version`, `-Protocol`, `-Extensions`; forward no cookies or
  other headers), relay go2rtc's response bytes to the client, then pump bytes both ways until
  either side closes (two threads or `selectors`; set `TCP_NODELAY`; close both sockets on
  exit). Cap concurrent WebSocket relays at 64 → 503 beyond that.
  After relaying the 101, also forward any bytes already buffered in the handler's `rfile`
  before switching to raw socket reads, and set `close_connection = True` so the handler
  thread ends when the relay ends. If go2rtc is restarting (socket missing/refused) → 503.
- Keep-alive correctness: every non-upgrade response carries `Content-Length` (or
  `Connection: close`), including error bodies and 204s.
- Mutations are serialized with one lock (discovery + probe + store + go2rtc call).
- Background thread every 60 s: re-run `ip_for_mac` (ARP only, no sweep) for each camera; if a
  camera gained an IP or its IP changed, `apply` once with the updated list (at most one
  restart per pass; no restart when nothing changed).
- `__main__`: `cd` to repo root; read `.env` (`KASA_EMAIL`, `KASA_PASSWORD` required,
  `DASHBOARD_PORT` default 1984); load/seed the store; resolve IPs (one sweep if any missing);
  start go2rtc; start the server; print `Dashboard: http://127.0.0.1:<port>/` and one line per
  camera (name + IP or "not found"); `--open` opens the browser after the server is listening.
  Zero cameras is **allowed** now (the Setup panel adds them). Clean shutdown on SIGINT/SIGTERM.
  Port already in use → clear message, exit 1.
- Never print or log the email, password, or their encoded forms.

### 6. `start.sh`
Keep: `set +x`, `set -euo pipefail`, `cd` to script dir, `.env` existence check, run
`scripts/setup.sh` when `bin/go2rtc` is missing. Then
`exec /usr/bin/python3 -m app "$@"`. Nothing else.

### 7. `www/dashboard.js` — minimal change only (job 2 rebuilds the page)
Load the camera list from `api/cameras` (`response.cameras`) instead of `cameras.json`.
Nothing else changes in `www/`. `www/cameras.json` is no longer generated (remove it from
`.gitignore`; add `data/` and `__pycache__/`).

### 8. Tests (`/usr/bin/python3 -m unittest discover -s tests -t . -v`)
Besides the ports in §2: store (seed, id allocation never reuses, limits, name validation,
atomic save, corrupt-file recovery); `render_config` (placeholders literal, skipped cameras,
no TCP listen lines); `apply` re-renders before each spawn (fake child process); the go2rtc
  client never issues PUT/PATCH/DELETE (assert on the fake upstream's request log); socket-path fallback for a long repo path; and `tests/test_server.py`
running the real handler against a **fake go2rtc** (a tiny Unix-socket HTTP server defined in
the test) on an ephemeral port: static file served; traversal 404; `GET /api/cameras`;
POST happy path with discovery and probe injected as fakes; each POST error code; DELETE;
PUT layout; a WebSocket upgrade relayed end-to-end (fake upstream answers 101 and echoes
bytes); a foreign-Origin POST never reaches the fake go2rtc; no response ever carries an
`Access-Control-Allow-Origin` header. Structure the server so discovery/probe/go2rtc client are
injectable for these tests. Tests must not need network, the real cameras, `.env`, or
`bin/go2rtc`.

## Constraints
- Python **3.9**, stdlib only (macOS `/usr/bin/python3`): no `match`, no `X | Y` type syntax,
  no third-party packages.
- Credentials never on a command line, in a generated file, in `www/`, in logs or in error
  text. The login reaches go2rtc only through its environment variables.
- Only listener: `127.0.0.1:<port>` (Python). go2rtc: Unix socket only.
- No dashboard or go2rtc from an earlier version is running; do not start one against the real
  cameras, do not run `scripts/setup.sh`. The reviewer does the live check.
- Do not commit; leave changes in the working tree.

## Done when
- [ ] `/usr/bin/python3 -m unittest discover -s tests -t . -v` passes, including the untouched
      `tests/test_security.py`.
- [ ] `/bin/bash -n start.sh` is clean; `git diff --stat HEAD -- tests/test_security.py` is empty.
- [ ] `grep -rn "KASA_PASSWORD\|KASA_EMAIL\|api/streams\|api/config" www/index.html www/dashboard.js www/dashboard.css` finds nothing.
- [ ] Final message lists files changed and what you could not verify.

## Context Codex needs
- Verified on this machine with go2rtc 1.9.15: `api.listen: ""` + `api.unix_listen: "<path>"`
  gives zero TCP/UDP listeners; over the socket `GET /api/streams` → 200;
  `PUT /api/streams?name=X&src=<url>` → 200 **and rewrites the config file with that URL**
  (hence the restart design); and a WebSocket upgrade to `/api/ws?src=X`
  → 101 both with `Origin: http://localhost` + `Host: localhost` and with no Origin.
- Cameras: `AA:BB:CC:DD:EE:01` (192.168.1.178) and `AA:BB:CC:DD:EE:02` (192.168.1.147); both
  answer the same Kasa login on :19443 (HTTP Basic, `email:base64(password)`, self-signed TLS,
  `multipart/x-mixed-replace` H.264). A camera returns `503 Service Unavailable` to a second
  simultaneous connection.
- The page (`www/vendor/video-rtc.js`) opens `ws://<host>/api/ws?src=<id>`, sends JSON text
  frames (`{"type":"mse",...}`) and receives text + binary frames; the relay must be transparent.
