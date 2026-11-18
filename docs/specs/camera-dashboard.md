# Codex task spec — Kasa camera dashboard

Effort: high

## Goal
A one-command local dashboard (`./start.sh`) that shows a TP-Link Kasa camera's live video in the
browser at `http://127.0.0.1:1984/`, using go2rtc as the stream bridge.

## Scope — touch ONLY these (all new unless noted)
- `start.sh` — launcher
- `scripts/lib.sh` — pure shell helpers, sourced by `start.sh` and the tests
- `scripts/setup.sh` — downloads the pinned go2rtc binary into `bin/`
- `go2rtc.yaml` — go2rtc config (no secrets in it)
- `www/index.html`, `www/dashboard.css`, `www/dashboard.js` — the dashboard page
- `tests/test-lib.sh` — tests for `scripts/lib.sh`
- `.env.example`, `README.md`

Do NOT modify: `www/vendor/video-rtc.js`, `www/vendor/video-stream.js` (vendored from go2rtc
v1.9.15, MIT), `.gitignore`, `.env`. Do NOT read or print `.env` contents.

## What to do

### 1. `scripts/setup.sh`
- Pinned version `v1.9.15`. Pick the asset by `uname -m`:
  - `arm64` → `go2rtc_mac_arm64.zip`, sha256 `d0cf858a71803dce2377423bc830639137ee96c335f88e9912e7f7898374266d`
  - `x86_64` → `go2rtc_mac_amd64.zip`, sha256 `7ca6075421b824eebe0383d20bd42a6f6a7ec7591a60931d42290f1d375169b9`
- URL: `https://github.com/AlexxIT/go2rtc/releases/download/v1.9.15/<asset>`.
- Download to a temp dir, verify with `shasum -a 256`; on mismatch delete the download and exit
  non-zero with a clear message. Unzip (the zip holds a single file `go2rtc`), `chmod +x`, move to
  `bin/go2rtc`. If `bin/go2rtc` already exists and `bin/go2rtc -version` reports 1.9.15, do nothing.
- Non-macOS → exit with "unsupported platform".

### 2. `scripts/lib.sh` (pure functions, no side effects when sourced)
- `env_get FILE KEY` — print the value of `KEY=` from a dotenv file **without `source`/`eval`**
  (passwords may contain `$`, backticks, quotes, spaces, `#`, `=`). First match wins; take everything
  after the first `=`; strip a trailing `\r`; strip one pair of matching surrounding single or
  double quotes; otherwise keep the value byte-for-byte. Missing key → print nothing, return 1.
- `urlencode STRING` — percent-encode everything except `A-Za-z0-9-._~` (so `@` → `%40`,
  `+` → `%2B`). Pure bash or `python3` is fine (system python 3.9 is available).
- `b64 STRING` — base64 of the exact bytes, no trailing newline, no line wrapping.
- `normalize_mac STRING` — lowercase, accept `:` or `-` separators, and **strip leading zeros in
  each octet** to match macOS `arp -an` output (e.g. `0C:70:43:AD:C0:B6` → `c:70:43:ad:c0:b6`,
  `AA:BB:CC:DD:EE:01` → `aa:bb:cc:dd:ee:01`). Invalid MAC → return 1.
- `ip_for_mac MAC [ARP_OUTPUT]` — given `arp -an` text (second arg, or run `arp -an` when omitted),
  print the IP of the entry whose MAC matches after normalization; no match → return 1.
  `arp -an` lines look like: `? (192.168.1.178) at aa:bb:cc:dd:ee:01 on en1 ifscope [ethernet]`
  and unresolved ones like `? (192.168.1.202) at (incomplete) on en1 ifscope [ethernet]`.

### 3. `start.sh`
1. `cd` to the script's own directory. `set -euo pipefail`.
2. Require `.env`; read `KASA_EMAIL`, `KASA_PASSWORD` (both required — exit with a message naming
   the missing key, never its value), `CAMERA_MAC` (required unless `CAMERA_IP` is set),
   optional `CAMERA_IP` (skips discovery), optional `DASHBOARD_PORT` (default `1984`).
3. If `bin/go2rtc` is missing, run `scripts/setup.sh`.
4. Resolve the camera IP: `ip_for_mac`; if not found, ping-sweep the local /24 of the default
   route's interface once (parallel `ping -c 1 -t 1`, background + `wait`) and retry; still not
   found → exit 1 with a message saying the camera with that MAC isn't on this network.
5. Export for go2rtc: `KASA_USER_ENC` (urlencoded email), `KASA_PASS_B64` =
   `urlencode "$(b64 "$password")"` (the camera expects base64; the base64 text is then
   percent-encoded because `/`, `+`, `=` are not safe in URL userinfo — verified to play),
   `CAMERA_IP`, `DASHBOARD_PORT`.
6. Print `Dashboard: http://127.0.0.1:<port>/` and the camera IP. Never print the email, password,
   or the encoded forms.
7. `exec bin/go2rtc -c go2rtc.yaml`. Optional `--open` flag: open the URL in the default browser
   (`open`) about a second after launch.

### 4. `go2rtc.yaml` (this exact shape was verified working against the real camera)
```yaml
api:
  listen: "127.0.0.1:${DASHBOARD_PORT:1984}"
  static_dir: "www"
rtsp:
  listen: ""      # disabled
webrtc:
  listen: ""      # disabled; the page uses MSE over the API websocket
streams:
  cam: kasa://${KASA_USER_ENC}:${KASA_PASS_B64}@${CAMERA_IP}:19443/https/stream/mixed?video=h264&audio=g711&resolution=hd
log:
  level: info
```
`${DASHBOARD_PORT:1984}` inside the quoted value is verified to substitute (default used when unset).

### 5. Dashboard page (`www/`)
- Plain HTML/CSS/ES modules, no build step, no CDN or other external requests.
- `index.html` loads `dashboard.js` as a module, which imports `./vendor/video-stream.js` (that
  registers the `<video-stream>` custom element; it imports `./video-rtc.js` next to it).
- Create the player with `mode = 'mse'` and `src = new URL('api/ws?src=cam', location.href)`
  (the component turns http→ws itself). Set `mode` **before** `src`. Muted autoplay.
- The page may call only `api/ws?src=cam` and `api/frame.jpeg?src=cam`. It must **never** request
  `api/streams` or `api/config`: both return the source URL including the encoded login.
- Layout: dark, single camera tile filling most of the viewport, 16:9, responsive down to phone
  width. Header: title "Camera", a status pill, a live clock.
- Status pill: `Connecting` / `Live` / `Reconnecting` / `Offline`. Drive it from real playback, not
  from the socket alone: `Live` only while the inner `<video>`'s `currentTime` has advanced within
  the last ~5 s; `Reconnecting` when it has stalled; `Offline` after ~30 s without frames. The
  vendored component already reconnects its websocket on its own when the socket closes (15 s
  timeout) — do not reimplement that. For a stall with the socket still open (> 30 s without
  frames), force a fresh connection at most once per 30 s by calling the element's
  `ondisconnect()` then `onconnect()`. Re-assigning `src` does NOT work for this: `onconnect()`
  returns early while `this.ws` is set.
- Hidden tabs: the component closes its connection ~5 s after `document.hidden` becomes true and
  reconnects on return (`visibilityCheck`, keep that default). So while `document.hidden`, pause
  the stall timer and do not force reconnects or show Offline; on return show `Connecting` and
  restart the stall clock.
- Controls under the tile: **Snapshot** (fetch `api/frame.jpeg?src=cam` and download it as
  `camera-YYYYMMDD-HHMMSS.jpg`), **Fullscreen** (on the tile). No audio controls: the browser stream is video-only (the camera's
  G.711 audio is not carried over MSE), so keep the video muted.
- Show "last frame: <n>s ago" next to the pill when not Live.
- Accessible: buttons are real `<button>`s with labels; visible focus; respects
  `prefers-reduced-motion`.

### 6. `tests/test-lib.sh`
Plain bash, no framework; exits non-zero on any failure and prints a pass/fail line per case.
Cover at least: `env_get` with a quoted value, an unquoted value containing `$`, `#`, `=`, a space
and a backtick, a CRLF line, a missing key; `urlencode` of `a.b+c@example.com`; `b64 secret1` =
`c2VjcmV0MQ==`; `normalize_mac` for upper-case, dash-separated and leading-zero inputs and an
invalid one; `ip_for_mac` against a pasted multi-line `arp -an` sample including an `(incomplete)`
row and a leading-zero-stripped MAC. Use temp files under `mktemp -d`; never touch the real `.env`.

### 7. `.env.example` and `README.md`
- `.env.example`: `KASA_EMAIL=`, `KASA_PASSWORD=`, `CAMERA_MAC=`, commented `# CAMERA_IP=`,
  `# DASHBOARD_PORT=1984`, with one-line comments.
- `README.md`: what it is, setup (`cp .env.example .env`, fill in, `./start.sh --open`), how it
  works (camera :19443 → go2rtc → MSE in the browser), that it binds to 127.0.0.1 only and why
  (go2rtc's API has no login here), and troubleshooting (camera not found, 401 = wrong login,
  restart `start.sh` if the camera's IP changes).

## Constraints
- bash 3.2 compatible (macOS `/bin/bash`): no associative arrays, no `mapfile`, no `${var,,}`.
- No new dependencies beyond what macOS ships plus the go2rtc binary. No Node, no npm.
- Credentials never appear in stdout/stderr, in `go2rtc.yaml`, in `www/`, or on any command line
  (pass them to go2rtc through the environment only).
- All listeners bind to `127.0.0.1`.
- You may not have network or camera access in your sandbox. Do not block on that: do not run
  `scripts/setup.sh` or `start.sh` against the real camera; the reviewer does the live check.
- Do not commit; leave changes in the working tree.

## Done when
- [ ] `/bin/bash tests/test-lib.sh` passes.
- [ ] `/bin/bash -n start.sh scripts/lib.sh scripts/setup.sh` is clean.
- [ ] `grep -rn "KASA_PASSWORD\|KASA_EMAIL" www go2rtc.yaml` finds nothing.
- [ ] Your final message lists the files written and anything you could not verify.

## Context Codex needs
- Camera: Kasa, MAC `AA:BB:CC:DD:EE:01`, currently `192.168.1.178` (DHCP, may change). Only port
  19443 matters. It answers `GET /https/stream/mixed?...` over HTTPS (self-signed) with HTTP Basic
  auth `email : base64(password)` and a `multipart/x-mixed-replace` body of H.264 parts
  (2304×1296, ~15 fps).
- go2rtc 1.9.15 facts verified on this machine: the `kasa://` source above plays; `${VAR}` and
  `${VAR:default}` in the yaml are replaced from the environment; `api.static_dir: "www"` serves
  that folder at `/` and **replaces** go2rtc's built-in web files (which is why the two player
  scripts are vendored in `www/vendor/`); `rtsp.listen: ""` and `webrtc.listen: ""` disable those
  listeners; `GET /api/frame.jpeg?src=cam` returns a JPEG; `GET /api/streams` returns JSON.
- `www/vendor/video-rtc.js` is the base `VideoRTC` class (properties `mode`, `src`, `background`,
  `visibilityCheck`; it creates an inner `<video>` available as `.video`);
  `www/vendor/video-stream.js` defines `<video-stream>`. Read both before writing `dashboard.js`.
