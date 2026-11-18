# Codex task spec — Wall-display redesign: edge-to-edge grid, status bar, Spotlight layout

Effort: high

## Goal
Restyle the dashboard for an unattended wall monitor: video fills the screen edge to edge, one
slim always-visible status bar replaces the header, controls appear only while someone is using
the page, and a new "Spotlight" layout shows one camera large with the rest in a side strip.

## Scope — touch ONLY these
- `www/index.html`, `www/dashboard.css`, `www/dashboard.js`
- `app/store.py` (allow the new layout value only)
- `tests/test_store.py` (extend the layout test for the new value)
- `README.md` ("Using the dashboard" section, and the API table's layout list)

Do NOT modify `app/server.py`, `app/security.py`, `app/go2rtc.py`, `tests/test_security.py`,
`www/vendor/*`, `start.sh`, `scripts/*`. No new files.

## What to do

### 1. Page structure: no header, one bottom status bar
Remove the top header. The page is: the camera area filling the viewport, and a bottom status
bar (`<footer>` inside `<main>`, ~40 px tall at 1080p, scaling with `clamp()`/`vh` so it stays
readable on a 4K panel viewed from across a room).

The status bar has two groups:
- **Always visible (left):** clock `HH:MM` (large, tabular numerals), date (e.g. `Wed 18 Nov`,
  from `toLocaleDateString`), then health: `● 3 live` and, only when non-zero, `● 1 offline`
  (count of cameras on the visible page in the Offline state; "Connecting/Reconnecting" cameras
  are counted in neither, and shown as `● 1 connecting` only while non-zero). Existing transient
  messages ("Restarting video…", "Dashboard server is not responding.", "Layout not saved" +
  Retry save) also live here and are always visible while active.
- **Controls (right), visible only while the page is "active":** paging (Previous / "Page 1 of 3"
  / Next, as today, hidden when one page), the layout segmented control, and Setup.

The existing `#summary` text content ("2 of 4 live · 6 cameras") is replaced by the health group;
keep a single `role="status"` element for it and update its text only when it changes.

### 2. Active vs idle
- The page is **active** for 4 s after any `pointerdown`, `keydown`, `wheel`, `touchstart`, or a
  `pointermove` whose `clientX/clientY` differ from the last seen position (browsers fire
  `pointermove` under a stationary cursor when layout changes; that must not wake an unattended
  display), and for as long as: the Setup dialog is open, focus is inside the status bar, or
  a pointer is over the status bar. Otherwise it is **idle**.
- Idle: set `data-idle="true"` on `.dashboard`; the controls group and per-tile buttons fade out
  (opacity + `visibility: hidden` after the transition so they leave the tab order), and the
  mouse cursor is hidden (`cursor: none`) over the camera area.
- Active: controls are fully visible and focusable. The first Tab press from idle must land on a
  visible control (the keydown makes the page active before focus moves).
- Respect `prefers-reduced-motion`: no fade, just show/hide.
- The page starts active (so a person opening it sees the controls) and goes idle after 4 s.
- **Never idle-hide the way out:** while there are no cameras, or the camera list failed to
  load, the page stays active. Keep the existing empty state (`#empty-state`, `#empty-title`,
  `#empty-description`, `#empty-setup`, `#retry-cameras`) centred in the camera area, working
  exactly as today.

### 3. Tiles: edge to edge
- Fixed layouts (2×1, 2×2, 3×3, 4×4) and Spotlight fill the whole area above the status bar with
  a 2 px gap between tiles, no outer padding, no rounded corners, no tile borders or headers.
  Video is `object-fit: contain` on a black tile (cameras have different aspect ratios).
- Overlay, bottom-left of each tile: a status dot + camera name, on a translucent dark scrim
  chip so it is readable over bright video. Dot colours: Live = green, Connecting/Reconnecting =
  amber, Offline = red. State must not be colour-only: when not Live, append the state in words
  (`Garage · reconnecting`, `Garage · offline 4 min` — minutes since the tile left Live, updated
  by the existing 1 s tick, minute granularity, `offline <1 min` at first).
- An Offline tile also gets a 2 px red inset outline so it is visible from across the room.
- Snapshot and Fullscreen: icon-style buttons bottom-right of the tile. They are real
  `<button>`s that stay in the tab order whenever the page is active; they are visually revealed
  when the tile is hovered or `:focus-within`. These two buttons are the only focusable elements
  in a grid tile or the Spotlight large tile. They keep text accessible names
  (`aria-label="Snapshot of <name>"`); draw glyphs with CSS or inline SVG created via DOM APIs
  (no `innerHTML`, no icon fonts, no external files).
- Empty slots: plain black with a faint "Empty" label; no label at all when idle.
- `Auto` keeps today's responsive, scrollable grid but adopts the same overlay labels, 2 px gaps
  and black tiles. Below 700 px width every layout falls back to Auto, as today.
- Double-click on a grid tile or the Spotlight large tile toggles fullscreen for that tile
  (keyboard users use the Fullscreen button). Strip tiles are different — see section 4.
- A camera with `available: false` (not found on the network) shows a red dot and
  `<name> · not found` and holds no connection. "offline N min" counts from the moment the tile
  last left Live, or, if it has never been Live, from when it first became visible.

### 4. Spotlight layout
- New layout value `spotlight` (button label "Spotlight", placed after `4×4`), saved through the
  existing `PUT api/layout`. Add it to `LAYOUTS` in `app/store.py` and to its error message, and
  extend the layout loop in `tests/test_store.py`.
- Geometry: one large tile on the left (3fr) and a vertical strip on the right (1fr) holding up
  to **4** small tiles in 4 equal rows, 2 px gaps. With a single camera, the large tile takes the
  full width and the strip is not rendered.
- The spotlight camera is the one whose id is stored in `localStorage['kcd.spotlight']`; if that
  id is missing or no longer exists, it is the first camera in configured order. Wrap every
  `localStorage` read/write in try/catch; the page must work without it.
- The other cameras, in configured order, fill the strip; more than 4 → the strip pages with the
  existing paging controls and arrow keys (`pageCount = ceil((n - 1) / 4)`, min 1). The large
  tile stays put across pages.
- A strip tile has exactly one control: a transparent `<button>` covering the whole tile with
  `aria-label="Show <name> large"`. Clicking it (or Enter/Space) makes that camera the spotlight
  camera and saves the id. Strip tiles have **no** Snapshot or Fullscreen buttons and no
  double-click behaviour; those exist only on the large tile and on grid tiles. Build the tile so
  the same node can be either kind (toggle with a class / `hidden` on the controls).
- **Do not move tile nodes in the DOM to swap.** Keep all tiles as children of the one existing
  grid container in configured order and place them with CSS: the spotlight tile gets a class
  that puts it in column 1 spanning all rows; the others auto-place into column 2. A swap is a
  class change plus re-running the visibility pass, so no `<video-stream>` element is detached
  and re-attached (re-insertion triggers the vendored element's `connectedCallback`, which would
  reconnect tiles that were deliberately disconnected). After a swap, move keyboard focus to the
  new large tile's Fullscreen button.
- Tiles on a hidden strip page disconnect and reconnect exactly as hidden-page tiles do today.
- The health counts cover the large tile plus the visible strip page.

### 5. Setup panel
Keep the `<dialog>`, its fields, ids, validation, messages and behaviour. Restyle only:
- Right-side sheet, full height, width `min(420px, 100vw)`, same dark surface family as the
  status bar; clear section titles; inputs and buttons at least 40 px tall (usable with a
  touch screen); the camera list as bordered rows, each showing name, MAC, IP or "not found on
  the network", and the same status dot colours as the tiles.
- Opening Setup keeps the page active; closing returns focus to the Setup button (as today).

### 6. Visual language
- One dark, neutral palette defined as CSS custom properties on `:root` (background black for
  video areas, near-black surfaces for bar/sheet, one accent colour for focus rings and the
  selected layout, green/amber/red for state). No gradients, glows or drop shadows except a
  subtle scrim behind overlay labels.
- System font stack; tabular numerals for the clock; minimum 4.5:1 contrast for text; visible
  `:focus-visible` rings on every control.
- Sentence case labels, no exclamation marks, no emoji.

### 7. Keep (regression list — all must still work)
Per-tile playback-progress status logic and timings; hidden-tab pause; forced reconnect after a
30 s stall; paging with only visible tiles connected; reconcile-by-id on add/remove without
recreating surviving players; the "Restarting video…" window after add/remove; 5 s retry of
`api/cameras`; layout save failure handling; the daily reload (`dailyReload`, unchanged);
`textContent` only; strict CSP (no inline scripts or event-handler attributes — inline `style`
attributes are allowed, but prefer classes and CSS custom properties set via `style.setProperty`);
requests limited to `api/cameras`, `api/cameras/<id>`, `api/layout`, `api/ws?src=<id>`,
`api/frame.jpeg?src=<id>`.

## Not in this build
- No automatic rotation of the spotlight camera on a timer.

## Constraints
- Plain ES modules, no build step, no dependencies, no external requests, no new files.
- Do not change element ids that `dashboard.js` or the README rely on unless you update every
  use; the Setup dialog's ids must stay exactly as they are.
- A dashboard is running from the main checkout on port 1984. Do not stop it, start another on
  that port, or send it write requests. The reviewer does the live check.
- This repository is public: placeholder MACs only (`AA:BB:CC:DD:EE:01`), no real names or IPs.
- Do not commit; leave changes in the working tree.

## Done when
- [ ] `/usr/bin/python3 -m unittest discover -s tests -t .` passes.
- [ ] `git diff --stat HEAD -- tests/test_security.py app/server.py app/security.py app/go2rtc.py www/vendor` is empty.
- [ ] `grep -n "innerHTML\|insertAdjacentHTML\|document.write\|eval(\|api/streams\|api/config" www/index.html www/dashboard.js` finds nothing.
- [ ] `node --check www/dashboard.js` passes if `node` is available (skip silently otherwise).
- [ ] Final message lists files changed, any ids renamed, and what you could not verify.

## Context Codex needs
- Current files: `www/index.html` (142 lines), `www/dashboard.css` (735), `www/dashboard.js`
  (743). Layout list lives at `www/dashboard.js:15` and `app/store.py:17`; page maths in
  `renderPage()`; fixed-layout fallback in `effectiveLayout()`.
- `app/store.py` treats a state file whose layout is not in `LAYOUTS` as corrupt; adding the value
  is the only backend change needed. `app/security.py` does not inspect the layout value.
- The wall display is a 1920×1080 monitor running one browser window for weeks; today it shows
  four cameras with native sizes 2304×1296 and 2560×1440.
