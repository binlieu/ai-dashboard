# Kasa camera dashboard

A local macOS wall-display dashboard for up to 16 Kasa cameras sharing one Kasa
account: live video in grid or Spotlight layouts, automatic camera rotation, a
clock and weather tile, selectable themes, and landscape or portrait screens.
Cameras are added by MAC address in the built-in Setup screen, and video stays
on your network.

`./start.sh` serves **http://127.0.0.1:1984/** with Python 3.9 and the go2rtc
video bridge. The bridge is **go2rtc v1.9.15** on macOS 13 and newer and
**v1.9.14** on macOS 12. Both releases are pinned by SHA-256 checksum. Python
uses only its standard library, and the page has no build step, CDN, Node or npm
dependency.

## Install on a dedicated Mac

Follow the [macOS Monterey installation guide](docs/INSTALL-MAC.md) to start
the dashboard at login, open it on a wall display, and transfer settings from
another Mac.

## Setup

Requires an Apple Silicon or Intel Mac, system `/usr/bin/python3`, a Kasa
account, and cameras on the same local network. The first launch downloads
go2rtc from GitHub and verifies its pinned SHA-256.

```sh
cp .env.example .env
```

Fill in `KASA_EMAIL` and `KASA_PASSWORD`. Values are literal: `$`, backticks,
spaces, `#` and `=` are preserved. One matching pair of outer quotes is removed;
the first occurrence of a key wins. Do not use `export` or spaces around `=`.
Keep `.env` private. Set `DASHBOARD_PORT` to change the default port, `1984`.

On the **first run only**, `CAMERA_MACS` can seed a comma-separated MAC list.
Colons or dashes, uppercase or lowercase, and 1–2 hex digits per octet are
accepted. Spaces around commas are removed. `CAMERA_NAMES` supplies positional
names; missing or empty names default to `Camera N`. Names are trimmed, must
contain 1–40 characters, and must not contain control characters. Commas delimit
entries, so they cannot appear inside a seed name. Invalid or duplicate MACs
stop initial seeding. Leave `CAMERA_MACS` blank to start with zero cameras.
When `CAMERA_MACS` is absent, the old `CAMERA_MAC` seeds one camera.

After `data/cameras.json` exists, camera settings in `.env` are ignored.
`CAMERA_IP` and `CAMERA_IPS` overrides are no longer used: discovery follows each
stored MAC. The Kasa login and dashboard port continue to come from `.env`.

If a MAC belongs to a non-camera Kasa/TP-Link device, Setup names its kind and
model in the error when available. Identification sends two small local UDP
questions to that one device, on ports 9999 and 20002, retrying once if needed
within 1.5 seconds. Nothing leaves the local network.

```sh
./start.sh --open
```

Omit `--open` to leave the browser closed. Press **Ctrl+C** to stop; SIGTERM also
shuts down the front server, video relays and child process. The launcher works
from any directory. A port already in use produces an error instead of opening
another camera connection. Do not run multiple bridges against the same camera.

## Private bridge and persistent state

```text
Browser → Python on 127.0.0.1:<port> → private Unix socket → go2rtc → Kasa :19443
```

Python is the only TCP listener. go2rtc has its HTTP, RTSP and WebRTC network
listeners disabled; its API is available only through a private Unix socket.
Python serves `www/` and forwards only known camera IDs to the video WebSocket
and JPEG endpoints. go2rtc's writable API is never exposed to the browser.

Every front-server request passes a strict Host/path/method allowlist. API
requests carrying foreign Origin or cross-site Fetch Metadata are denied.
WebSocket upgrades and mutations require an exact same-origin Origin; writes
with JSON bodies require `application/json`. Responses have restrictive CSP,
same-origin resource policy, no-store, nosniff and no-referrer headers, and no
CORS allow headers. This protects local API writes from unrelated browser pages.
Other programs running as your macOS user can still access local services.

Generated files:

- `data/cameras.json`: versioned camera IDs, names, canonical MAC addresses,
  next-ID counter, layout and optional weather settings. The directory has mode `700`, file mode `600`.
  Writes use a same-directory temporary file and atomic replacement. IDs are
  never reused, including after removal. A corrupt file is renamed to a private
  `cameras.json.corrupt-<timestamp>` backup; startup warns and uses an empty list.
  Invalid weather settings alone reset weather and warn while preserving cameras.
- `run/go2rtc.yaml`: Unix-socket-only configuration, mode `600` in a mode `700`
  directory. Its login placeholders stay literal; real login values reach
  go2rtc only through its environment. Config is rebuilt before every child
  spawn, including crash recovery. No login values are written to generated files.
- `run/api.sock`: normally the bridge socket. If its absolute path would exceed
  100 bytes, a private `/tmp/kcd-*` directory holds the socket instead. Stale
  socket files are removed at startup; owned sockets and temporary directories
  are cleaned on shutdown. `www/cameras.json` is no longer generated.

The Kasa stream uses HTTPS port 19443, HTTP Basic auth with
`email:base64(password)`, and the device's self-signed certificate. Login URL
components are encoded in the child's environment. Bridge log output masks
Kasa URL userinfo and login values. The add-camera probe reads headers only;
its legacy TLS fallback is scoped to that one device connection.

Startup resolves stored MACs from ARP. If any are missing, it makes one bounded
parallel ping sweep of the default route's local /24 and retries only missing
cameras. Unresolved cameras remain in the list as unavailable. Every 60 seconds,
an ARP-only check finds new or changed IPs and applies all changes in one restart;
an expired ARP cache entry alone does not tear down an existing stream.

Stream changes rebuild the private config and restart go2rtc, interrupting all
video briefly. The child must answer over its Unix socket within ten seconds.
Unexpected child exits retry after 1, 2, 5, then 10 seconds; the backoff resets
after a stable run. The front server stays available during crash recovery.

## Using the dashboard

Open **Setup** in the bottom status bar to manage cameras. With an empty camera list, the
centred **Open Setup** button takes you to the same panel. Close the panel with
**Close** or **Esc**; focus returns to Setup.

**Themes:** in **Setup → Theme → Look**, choose **Default**, **Mission control**,
**Starship HUD** or **Deep space observatory**. The choice is saved per display
and applies immediately without interrupting video.

- **Add:** enter the camera's MAC address, with colons, dashes or 12 hexadecimal
  digits without separators. Find it in the Kasa app under Device settings →
  Device info, or on the camera's label. A name is optional. While the server
  searches, the form is disabled and shows progress; errors leave your entries
  intact so you can correct or retry them. The panel shows the configured count
  and disables adding at the server's camera limit.
- **Rename:** choose **Edit** beside a camera, enter its new name, then **Save**
  or **Cancel**. Renaming updates labels without restarting video.
- **Remove:** choose **Remove**, then confirm inside the panel. Adding or
  removing briefly restarts the video engine for all cameras. A single
  “Restarting video…” banner disappears when visible cameras are Live again or
  after ten seconds; the existing players reconnect automatically.

**Plugs and switches:** in Setup, add up to six devices by MAC address, and
rename or remove them from the same list. Their status-bar buttons show the
reported state. Press **On** or **Off** to switch the device, or **?** to refresh
its state. Anyone using this screen can switch the saved devices.

**Backup:** open **Setup → Backup**, enter the full path to an existing writable
folder, and choose **Keep 1 day**, **Keep 7 days**, or **Keep 30 days** for each
camera you want to record. Select **Save backup settings** to apply the choices.
Shortening retention asks for confirmation before older footage is deleted.
Choose **Off** to stop recording for a camera and leave its files in place.
The section shows the estimated storage needed, available drive space, and each
camera's recording status and last file time. The status bar shows when backup
is on and when the drive is missing or almost full.

The video area fills the screen above a slim, always-visible status bar with the
local time, date, and live, offline and connecting counts for the visible cameras.
Camera labels show a status dot and state in words when video is not live;
offline tiles have a red inset outline and show minutes since leaving Live (or
first appearing if never live). Cameras missing from the network show **not found**
and hold no video connection. Server, restart and layout-save messages remain
visible in the status bar.

The status bar and its controls (paging, plugs and switches, layout, Setup) stay
visible at all times. The Snapshot and Fullscreen buttons on each tile fade out
after four seconds without interaction; move the pointer, click, scroll, touch, or
press a key to reveal them. Idle displays
hide the cursor over video and hide **Empty** labels; reduced-motion preferences
disable the fade.

Choose **Auto**, **2×1**, **2×2**, **3×3**, **4×4** or **Spotlight** in the status bar.
Fixed layouts mean columns × rows, fill the video area, and letterbox video
inside black cells separated by 2 px gaps. Unused cells show **Empty** while
active. When there are more cameras than slots, use
**Previous / Next** or the **Left / Right arrow** keys to page (arrow shortcuts
are inactive while typing or using Setup). Only the visible page keeps video
connections open. Removing cameras clamps the page to the remaining range.

**Portrait displays:** monitors at least 700 px wide automatically use fewer columns
and more rows when mounted vertically. Spotlight places the large camera above a
2×2 strip; the info tile stays in the last cell. Layout names and saved choices stay
the same. Controls wrap below the status information when needed. Narrower screens keep the existing Auto fallback.

**Spotlight** keeps one camera large on the left and up to four others in a
vertical strip on the right. Select a strip tile with a click, Enter or Space to
show it large; keyboard focus moves to its Fullscreen button. The selection is
remembered in this browser when local storage is available, otherwise it lasts
for the current visit. If the selected camera is removed, the first configured
camera takes its place. Paging changes only the strip, and a single camera fills
the whole video area.

In Setup, **Spotlight rotation** can change the large camera every 10 seconds,
30 seconds, minute or 5 minutes; it defaults to **Off** and is remembered on this
display when local storage is available. Rotation chooses only Live cameras on
the visible strip page and pauses while the tab is hidden, Setup is open, or any
element is fullscreen. Manual selection restarts the interval; automatic changes
leave the controls idle and keyboard focus unchanged.

In Setup, **Clock and weather** adds an optional info tile. Check **Show the info
tile**, enter a US **ZIP code or city** (for example `90210` or `Springfield`),
choose **°F, mph** or **°C, km/h**, and select **Save**. The resolved place name is
shown after saving so you can confirm the match. Failed saves preserve your input.
The tile shows a large local clock and date, current conditions, feels-like
temperature, wind, and up to five forecast days with rain chances of at least 20%.
Small tiles hide forecast days that do not fit. The clock continues to work while
weather loads or is unavailable.
Once a forecast arrives, both clocks and dates follow the weather place's time zone.
They use computer time while the tile is off or until a forecast has arrived for the saved place since the server started.

The info tile uses the last cell on every fixed-layout page, leaving one fewer
camera slot. In Spotlight it occupies the fourth strip row, leaving three strip
camera slots; one camera stays large with the info tile alone in the strip. In
Auto it follows the cameras. An empty dashboard keeps its existing empty state.
Weather saves do not restart the video bridge. Forecasts refresh every 15 minutes
(retrying after two minutes on failure); the last good forecast remains available
for up to three hours. The browser checks the local cache every five minutes and
briefly polls while the initial forecast loads.

**Weather privacy:** the place you type and its coordinates rounded to two
decimals (about 1 km) are sent from this computer to Open-Meteo
(`open-meteo.com`) to look up the forecast. Nothing else is sent, and the browser
itself makes no external requests. Requests use a fixed application User-Agent;
the weather provider also sees the connection's public IP address. Disabling the
tile stops further weather requests; saving a different place while disabled
stores it locally for lookup when enabled.

Weather data by Open-Meteo.com (CC BY 4.0). Open-Meteo's free service is for
non-commercial use.

Hover over or focus a grid tile or the large Spotlight tile to reveal its
**Snapshot** and **Fullscreen** icon buttons. Both buttons are keyboard accessible
while active. Double-click the video area to toggle fullscreen. Strip tiles only
select the large camera and have no Snapshot or Fullscreen controls.
A full-screen camera shows the time in a top-left overlay about a quarter of the screen wide and, when weather is available, adds current conditions there and a five-day forecast along the bottom, all in black text at 80% opacity with no background.

The selected layout is saved across restarts. If saving fails, the new layout
stays on screen with **Layout not saved** and **Retry save**. Below 700 px the
display uses Auto while retaining the selected layout for wider screens. Auto
keeps the responsive grid and can scroll on smaller screens or larger lists.

Camera-list failures retry every five seconds, preserving existing tiles and
unfinished edits; successful refreshes also pick up camera availability changes.
Network failures show “Dashboard server is not responding.” No reload is needed
to retry camera actions or layout saves.

For unattended wall displays the page reloads itself once a day between 04:00
and 05:00 local time, only while the server answers, and not while Setup or a
fullscreen tile is open.

## Backup

Select a backup folder and choose 1, 7 or 30 days of retention for each camera
you want to record. Selected cameras stream continuously while the dashboard is
running, even when no browser is open. Recordings contain video only, with no
sound, in 3-minute MP4 files. Allow about 8–15 GB per camera per day.

Choose an existing writable folder outside the dashboard project. The dashboard
never creates the backup folder itself. Completed files are organized as
`camN/YYYY-MM-DD/HH-MM-SS.mp4`, using the computer's local time. A file being
written ends in `.mp4.part`. Changing folders leaves the previous recordings
where they are. If the drive is disconnected, recording resumes after it
becomes available again.
Backup supports drives formatted as exFAT or FAT.
When the status bar says **Backup: files not saving**, a camera has failed to
save two consecutive recordings and the dashboard is still trying.

The dashboard removes recordings older than each enabled camera's retention
period. When free space drops below 5 GiB, it removes older recordings early,
oldest first, while always keeping the last 24 hours. Recording pauses if it
still cannot free enough space.
Cleanup is limited to the dashboard's own recording files and empty date
folders inside the marked backup folder. Disabling or removing a camera leaves
its recordings in place.

## Same-origin API

The dashboard uses these same-origin endpoints.

| Method | Path | Behavior |
| --- | --- | --- |
| GET | `/api/cameras` | Layout, max 16, and cameras with ID, name, MAC, IP and availability. |
| POST | `/api/cameras` | JSON `{ "mac": "...", "name": "..." }`; name optional. Discover, probe, save and restart; returns 201. |
| PATCH | `/api/cameras/camN` | JSON `{ "name": "..." }`; rename without restarting. |
| DELETE | `/api/cameras/camN` | Remove and restart; returns 204. |
| GET | `/api/devices` | Max 6 and saved plugs/switches with ID, name, MAC and cached `on`, `off` or `unknown` state; no IPs. |
| POST | `/api/devices` | JSON `{ "mac": "...", "name": "..." }`; name optional. Discover, identify, read and save; returns 201. |
| PATCH | `/api/devices/devN` | JSON `{ "name": "..." }`; rename a saved plug or switch. |
| DELETE | `/api/devices/devN` | Remove a saved plug or switch without changing its power state; returns 204. |
| PUT | `/api/devices/devN/state` | Exactly `{ "on": true }` or `{ "on": false }`; switch and return the reported state with ID and name. |
| PUT | `/api/layout` | JSON `{ "layout": "2x2" }`; allowed: `auto`, `2x1`, `2x2`, `3x3`, `4x4`, `spotlight`. |
| GET | `/api/weather` | Settings, cached forecast, and `timezone` (zone name when enabled and known, otherwise `null`), with status `off`, `loading`, `ok` or `unavailable`; never exposes coordinates. |
| PUT | `/api/weather` | JSON `{ "enabled": true, "place": "Springfield", "units": "f" }`; units `f` or `c`. Resolves enabled places and returns the same shape as GET. |
| GET | `/api/backup` | Saved folder, per-camera retention and recording status, cached disk usage, and any storage warning. |
| PUT | `/api/backup` | JSON `{ "folder": "/Volumes/Drive/Recordings", "cameras": { "cam1": 7 } }`. Set 1, 7 or 30 days per camera. Use 0 or omit a camera to turn its recording off. Returns the same shape as GET. |
| GET | `/api/frame.jpeg?src=camN` | JPEG relay, capped at 10 MB. |
| GET | `/api/ws?src=camN` | Same-origin WebSocket upgrade; transparent relay, at most 64 concurrent connections. |

Camera mutations are serialized across discovery, probing, storage and bridge changes.
Weather requests run independently of that lock; geocoding may take about ten
seconds. Weather validation errors return 400, `place_not_found` returns 404,
and `weather_unavailable` returns 502. Forecast reads never wait on the network.
POST reports `invalid_mac` (400), `duplicate`/`limit` (409), `not_found` (404),
`auth`/`not_kasa` (422), or `go2rtc` (502). A `not_kasa` message names the device
kind and model when local identification succeeds; otherwise it says the address
did not answer as a Kasa camera. If applying a stream change fails,
the store is restored and the previous bridge configuration is applied again.
Errors return `{ "error": "code", "message": "description" }` without login values.

Each tile monitors playback progress independently. Live requires progress in
the last five seconds; stalls become Reconnecting, then Offline after thirty
seconds. Hidden tabs pause monitoring. The vendor handles closed-socket recovery;
an open stalled socket is restarted at most once per thirty seconds, after its
old close handler completes. Snapshot saves a name-based JPEG filename;
Fullscreen expands the selected tile. Video stays muted because camera G.711
audio is not carried over MSE. Use a current browser with H.264 MSE support.

### Plugs and switches

The API supports up to six single-outlet Kasa plugs and switches saved by MAC
address. Control uses local TCP 9999 or KLAP over HTTP port 80 and the existing
Kasa login in `.env`; credentials and device IPs are never returned by the device
API or saved with devices. Names and MACs remain in the private state file, and
the setup API lists MACs. States are polled every 15 seconds, with unknown devices
retried every 3 seconds. Failed operations report `unknown`; a failed write is
not automatically repeated. Only connect appliances that are safe to switch
remotely. Multi-outlet devices are not supported. Device operations do not restart video.

## Troubleshooting

- **Camera not found:** check its MAC, power, Wi-Fi and the Mac's network. VPN
  routes, client isolation or other subnets can defeat the local /24 sweep.
- **Authentication rejected / 401:** check the Kasa login in `.env` and restart.
- **IP changed:** the background ARP check updates it within about a minute when
  visible in ARP. Restart to repeat full discovery, including the one sweep.
- **Corrupt state warning:** the original file is preserved as a private backup
  beside `data/cameras.json`; re-add cameras through the local API.
- **Port in use:** stop the conflicting service or choose another `DASHBOARD_PORT`.
- **503 / competing live viewers:** a camera serves only one upstream stream.
  Several browser tabs can share one go2rtc instance, but a second bridge or the
  Kasa app's live view may occupy the camera. A 503 probe is accepted as busy.
- **Low frame rate:** a camera has been seen dropping to 3–5 fps and later
  returning to 15 on its own; the cause was not established. Close competing
  live views and check Wi-Fi signal strength. Slow advancing playback stays Live.
- **Checksum mismatch:** installation stops and removes the failed download;
  retry on a trusted connection without bypassing verification.

## Local checks

```sh
/usr/bin/python3 -m unittest discover -s tests -t . -v
/bin/bash -n start.sh
git diff --stat HEAD -- tests/test_security.py
grep -rn 'KASA_PASSWORD\|KASA_EMAIL\|api/streams\|api/config' www/index.html www/dashboard.js www/dashboard.css
```

The acceptance security test must remain unchanged. The grep should produce no
matches (exit status 1). Tests use temporary state, synthetic credentials and a
fake go2rtc HTTP/WebSocket upstream; no real `.env`, cameras or go2rtc binary are
used. Integration tests use ephemeral loopback/Unix listeners where allowed;
when a sandbox blocks bind(), connected socket pairs run the same handler,
client and relay assertions without listening. Live TCP binding and real-camera
playback still need verification outside such a sandbox.

## License

MIT, see [LICENSE](LICENSE). The files in `www/vendor/` come from
[go2rtc](https://github.com/AlexxIT/go2rtc) v1.9.15 and are covered by its MIT
license, included as `www/vendor/LICENSE-go2rtc`.
