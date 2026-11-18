# Codex task spec — Setup panel and grid layouts (job 2 of 2)

Effort: high

## Goal
Let the user add a camera by typing its MAC address in the dashboard, rename/remove cameras,
and choose a grid layout (Auto, 2×1, 2×2, 3×3, 4×4) that is remembered across restarts.

## Scope — touch ONLY these
- `www/index.html`, `www/dashboard.css`, `www/dashboard.js`
- `README.md` (usage section only)

Do NOT modify `app/*`, `tests/*`, `www/vendor/*`, `start.sh`, `scripts/*`, `.env`. The server
API from job 1 is live-verified and final; if you believe it has a bug that blocks this job,
stop and describe it in your final message instead of changing Python.

## What to do

### 1. Data flow
- On load: `GET api/cameras` → `{layout, max, cameras:[{id,name,mac,ip,available}]}`.
- All writes are same-origin `fetch` with `Content-Type: application/json` and a JSON body
  (DELETE has no body and no content type). Errors come back as
  `{"error":"<code>","message":"<text>"}` — show `message` (via `textContent`) and keep the form
  filled in. Network failure → "Dashboard server is not responding."
- After every successful add / remove, re-fetch `api/cameras` and reconcile the tiles: keep
  existing player objects for cameras that are still present (match by `id`), create new ones,
  and destroy removed ones (call the element's `ondisconnect()`, clear timers, remove the
  node). Rename only updates labels.
- Adding or removing a camera restarts the video engine server-side: every tile's video drops
  for a few seconds and the vendored player reconnects by itself. During that window show a
  single unobtrusive banner "Restarting video…" (auto-hide after 10 s or when all visible tiles
  are Live again) and do not flash tiles to Offline.

### 2. Setup panel
- A "Setup" button in the header opens a right-side panel (a `<dialog>` or an
  `aria-modal` region with focus trap; Esc and a Close button close it; focus returns to the
  Setup button).
- **Add camera** form: "MAC address" (required; placeholder `AA:BB:CC:DD:EE:FF`; accept `:` or
  `-` or no separators — normalise 12 hex digits to colon form client-side before sending;
  inline validation message when it is not a MAC), "Name" (optional, max 40), an "Add camera"
  button. While the request runs (it can take ~10 s when the server has to search the network)
  disable the form and show "Looking for the camera on your network…". On 201 clear the form
  and announce "Added <name>." in a `role="status"` region.
  Helper text under the field: "Find it in the Kasa app under Device settings → Device info,
  or on the camera's label."
- **Cameras** list: one row per camera — name (inline rename: an Edit button turns the name
  into an input with Save/Cancel; `PATCH api/cameras/<id>`), MAC, IP or "not found on the
  network", a status dot, and Remove. Remove asks for confirmation in-panel ("Remove <name>?"
  with Remove / Cancel buttons — no `window.confirm`), then `DELETE`.
- Show "N of <max> cameras". When the limit is reached, disable the add form with a note.
- Empty state on the main page when there are no cameras: a centred message with a button that
  opens Setup.

### 3. Layouts
- A layout control in the header: segmented buttons `Auto`, `2×1`, `2×2`, `3×3`, `4×4`
  (`aria-pressed`). Selecting one applies immediately and saves with
  `PUT api/layout {"layout":"2x2"}`; if the save fails, keep the new layout on screen and show
  a small "Layout not saved" note.
- `Auto` = the existing responsive behaviour.
- Fixed layouts are `columns × rows` (2×1 = two columns, one row). The grid fills the viewport
  below the header with **no page scrolling** at 16:9; tiles keep the video's aspect ratio
  letterboxed inside their cell (`object-fit: contain`), with per-tile controls overlaid or
  compact enough that rows do not overflow. Below ~700 px width fall back to Auto behaviour.
- Slots = columns × rows. Cameras fill slots in configured order. Unused slots render a dim
  placeholder ("Empty").
- More cameras than slots → pages. Show "Page 1 of 3" with Previous / Next buttons in the
  header (hidden when there is one page); Left/Right arrow keys also page when focus is not in
  an input. Only tiles on the visible page hold a connection: when a tile leaves the visible
  page, disconnect its player (`ondisconnect()`) and pause its stall tracking; when it becomes
  visible, reconnect (`onconnect()`) and restart tracking in the `Connecting` state. (Each
  hidden live player would otherwise keep pulling a full HD stream.)
- The header summary counts only cameras on the visible page as live/not live, e.g.
  "2 of 4 live · 6 cameras".
- If the saved layout's page index is out of range after a removal, clamp it.

### 4. Keep from the current page
Per-tile status pill driven by playback progress, hidden-tab pause, forced reconnect after a
30 s stall (old socket's close → `onconnect()`), Snapshot and Fullscreen per tile, the clock,
`textContent` only (no `innerHTML`), no external requests. Requests are limited to
`api/cameras`, `api/cameras/<id>`, `api/layout`, `api/ws?src=<id>`, `api/frame.jpeg?src=<id>`.
The page is served with a strict CSP (`default-src 'self'`, `style-src 'self' 'unsafe-inline'`,
no inline scripts): no inline `<script>`, no inline event-handler attributes, no `eval`.

### 5. `README.md`
Document Setup (add by MAC, rename, remove, the brief video restart) and layouts/paging.

## Constraints
- Plain ES modules, no build step, no dependencies.
- A wall display will run this unattended: no dialogs that block forever, no state that
  requires a reload to recover; a failed `api/cameras` load retries every 5 s.
- A dashboard from job 1 may be running on port 1984 from this worktree. Do not stop it, start
  another, or send it write requests. The reviewer does the live check.
- Do not commit; leave changes in the working tree.

## Done when
- [ ] `/usr/bin/python3 -m unittest discover -s tests -t .` still passes (no Python changed).
- [ ] `git diff --stat HEAD -- app tests www/vendor start.sh scripts` is empty.
- [ ] `grep -n "innerHTML\|insertAdjacentHTML\|document.write\|eval(\|api/streams\|api/config" www/index.html www/dashboard.js` finds nothing.
- [ ] Final message lists files changed and what you could not verify.

## Context Codex needs
- Live-verified API behaviour (curl, real cameras): `POST api/cameras` → 201 in ~0.8 s for a
  camera already in the ARP table, ~5 s then 404 `not_found` for an unknown MAC, 409
  `duplicate`, 400 `invalid_mac`, 422 `not_kasa` for a non-camera device, 422 `auth` when the
  camera rejects the Kasa login; `DELETE` → 204; `PUT api/layout` → 200 / 400. Ids are never
  reused (the two cameras are currently `cam1` and `cam3`).
- Writes need the browser's automatic same-origin `Origin` header and the JSON content type;
  the server rejects anything else with 403 / 415.
- Current cameras: "Camera 1" (2304×1296) and "Test camera" (2560×1440), both ~15 fps.
