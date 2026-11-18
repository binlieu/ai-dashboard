# Kasa camera dashboard

A local macOS wall-display dashboard for up to 16 Kasa cameras sharing one Kasa
account: live video in grid or Spotlight layouts, automatic camera rotation, a
clock and weather tile, selectable themes, and landscape or portrait screens.
Cameras are added by MAC address in the built-in Setup screen, and video stays
on your network.

`./start.sh` serves **http://127.0.0.1:1984/** with Python 3.9 and a pinned
**go2rtc v1.9.15** bridge. Python uses only its standard library; the page has
no build step, CDN, Node or npm dependency.

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

The video area fills the screen above a slim, always-visible status bar with the
local time, date, and live, offline and connecting counts for the visible cameras.
Camera labels show a status dot and state in words when video is not live;
offline tiles have a red inset outline and show minutes since leaving Live (or
first appearing if never live). Cameras missing from the network show **not found**
and hold no video connection. Server, restart and layout-save messages remain
visible in the status bar.

Paging, layout and Setup controls fade out after four seconds without interaction.
Move the pointer, click, scroll, touch, or press a key to reveal them. Setup, focus
inside the status bar, or a pointer over the bar keeps controls visible. Empty
camera lists and camera-list failures also keep controls visible. Idle displays
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
the same. Controls wrap below the status information when needed and leave the bar
when idle. Narrower screens keep the existing Auto fallback.

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

## Same-origin camera API

The dashboard uses these same-origin endpoints.

| Method | Path | Behavior |
| --- | --- | --- |
| GET | `/api/cameras` | Layout, max 16, and cameras with ID, name, MAC, IP and availability. |
| POST | `/api/cameras` | JSON `{ "mac": "...", "name": "..." }`; name optional. Discover, probe, save and restart; returns 201. |
| PATCH | `/api/cameras/camN` | JSON `{ "name": "..." }`; rename without restarting. |
| DELETE | `/api/cameras/camN` | Remove and restart; returns 204. |
| PUT | `/api/layout` | JSON `{ "layout": "2x2" }`; allowed: `auto`, `2x1`, `2x2`, `3x3`, `4x4`, `spotlight`. |
| GET | `/api/weather` | Settings and cached forecast with status `off`, `loading`, `ok` or `unavailable`; never exposes coordinates. |
| PUT | `/api/weather` | JSON `{ "enabled": true, "place": "Springfield", "units": "f" }`; units `f` or `c`. Resolves enabled places and returns the same shape as GET. |
| GET | `/api/frame.jpeg?src=camN` | JPEG relay, capped at 10 MB. |
| GET | `/api/ws?src=camN` | Same-origin WebSocket upgrade; transparent relay, at most 64 concurrent connections. |

Camera mutations are serialized across discovery, probing, storage and bridge changes.
Weather requests run independently of that lock; geocoding may take about ten
seconds. Weather validation errors return 400, `place_not_found` returns 404,
and `weather_unavailable` returns 502. Forecast reads never wait on the network.
POST reports `invalid_mac` (400), `duplicate`/`limit` (409), `not_found` (404),
`auth`/`not_kasa` (422), or `go2rtc` (502). If applying a stream change fails,
the store is restored and the previous bridge configuration is applied again.
Errors return `{ "error": "code", "message": "description" }` without login values.

Each tile monitors playback progress independently. Live requires progress in
the last five seconds; stalls become Reconnecting, then Offline after thirty
seconds. Hidden tabs pause monitoring. The vendor handles closed-socket recovery;
an open stalled socket is restarted at most once per thirty seconds, after its
old close handler completes. Snapshot saves a name-based JPEG filename;
Fullscreen expands the selected tile. Video stays muted because camera G.711
audio is not carried over MSE. Use a current browser with H.264 MSE support.

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
