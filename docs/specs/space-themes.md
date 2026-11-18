# Codex task spec — Selectable themes: Mission control, Starship HUD, Deep space observatory

Effort: high

## Goal
Add a "Theme" choice to Setup. The current look stays the default; three space-themed looks can
be selected per display and apply instantly without interrupting video.

## Scope — touch ONLY these
- `www/index.html`, `www/dashboard.css`, `www/dashboard.js`
- `README.md` ("Using the dashboard": a short "Themes" paragraph)

No Python changes, no test changes, no new files, nothing under `www/vendor/`.

## What to do

### 1. Selector and storage
- Setup gets a section "Theme" (after "Clock and weather"): `<label for="theme-select">Look</label>`
  and `<select id="theme-select">` with options `Default` (`default`), `Mission control`
  (`mission`), `Starship HUD` (`hud`), `Deep space observatory` (`observatory`), plus help text
  "Applies to this display."
- Persist in `localStorage['kcd.theme']` (same try/catch pattern as `kcd.spotlight`); accept
  only the four values, anything else means `default`.
- Apply by setting `document.documentElement.dataset.theme` to the value (remove the attribute
  for `default`). Do this as the first statements of `dashboard.js`, before any rendering, and
  again on change. Changing the select applies immediately and announces "Theme set to
  <option text>." in `#setup-announcement`.
- A theme change is CSS only: no tile node is created, removed or re-inserted, no player is
  reconnected, no request is made.

### 2. How themes are built
- With no `data-theme` attribute the page must render **exactly as it does today**: do not change
  any existing rule's computed result for the default look. If you need new custom properties
  or hooks, give them defaults equal to today's values.
- Each theme is a block `:root[data-theme="<name>"] { … }` overriding the existing colour
  custom properties, followed by a small number of structural rules scoped under the same
  attribute selector. Keep each theme's rules together under a comment heading.
- **Tokens first.** Any colour in `www/dashboard.css` outside the `:root` block (hex, `rgb()`,
  `rgba()`), and any colour set from `www/dashboard.js` (for example on the weather icons), must
  be moved into a custom property whose default equals today's value, so themes can reach it.
- **Decoration hook.** `.camera-card.is-offline::after` already draws the offline outline; keep
  `::after` for the offline indicator. All theme frames/brackets are drawn on one permanent
  `aria-hidden="true"` element per tile (class `tile-frame`, created once when the tile is built,
  `pointer-events: none`, invisible in the default theme). The info tile gets the same element.
- System font stacks only (CSP blocks external fonts): monospace =
  `ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace`.
- Flat rendering: no glows, no blur, no drop shadows, no animations or transitions beyond the
  ones that already exist. Static decoration only (a wall display runs for weeks; avoid anything
  that repaints continuously).
- Decoration must never cover the picture: anything drawn over a camera tile stays within the
  outer 3 % of the tile's shorter side (labels excepted), and has `pointer-events: none`.
- Every theme styles: page background, tile frame and gaps, tile labels and status dot, the
  offline indicator, empty slots, Snapshot/Fullscreen buttons, the Spotlight strip, the info
  tile (clock, date, weather, forecast, icons), the status bar and its controls (including the
  pressed layout button and focus rings), notices, and the Setup sheet (inputs, selects,
  buttons, list rows, error and status text).
- A tile in browser fullscreen shows plain video: no frame decoration, no clipping.

### 3. The three looks

**Mission control (`mission`)** — ground-control telemetry.
- Near-black blue background (`#05080d`), panel surface `#101821`, primary text/accent amber
  (`#ffb347`), secondary text `#b4b2a9`, hairlines dim amber (`#5c4a2a`).
- Monospace everywhere outside form inputs; labels, status-bar text and info-tile captions in
  uppercase (`text-transform`, the underlying text is unchanged).
- Tiles: 3 px gaps, 1 px dim-amber border, and four amber corner brackets drawn with CSS on a
  pseudo-element (each bracket arm about 2.5 % of the tile's shorter side, minimum 10 px).
- Tile label is prefixed with a feed number in configured order — `CAM 01 · FRONT` — via a
  zero-padded `data-index` attribute on each tile (kept up to date when cameras are added or
  removed) and a CSS `::before` with `attr()`. Use the alt-text form
  `content: "CAM " attr(data-index) " · " / "";` so screen readers skip it. Square status dot.
- The connecting/reconnecting marker and word use a hue clearly different from the amber text
  (use `#7fb8ff`); live stays green.
- Offline tile: red border and brackets.

**Starship HUD (`hud`)** — cyan line-work.
- Background `#02060a`, panel surface `#0b1a24`, primary text `#7fe7ff`, line colour `#2aa9c9`.
- Tiles: 6 px gaps and the look of cut corners on the top-left and bottom-right (cut = about
  2.5 % of the shorter side, minimum 12 px). **Do not clip the tile, the video element or any
  button** (clipping would cut the Snapshot/Fullscreen buttons and focus rings, and costs GPU
  time on a small PC). Draw it entirely on the `tile-frame` overlay: a 1 px cyan ring made with
  `clip-path: polygon(evenodd, …)` (outer cut shape minus inner cut shape), plus two corner
  triangles in the page background colour covering the cut corners. Inset the tile label and the
  tile buttons so they sit clear of the cuts. Setup-sheet buttons keep ordinary rectangles with
  a 1 px cyan border.
- Labels: uppercase, letter-spacing 0.12em, regular weight (never lighter than 400), and one
  step larger than the default theme so thin strokes stay readable from across a room.
- Status marker is a diamond (a rotated square) instead of a dot.
- Offline tile: the frame ring turns red.
- Clock numerals in the info tile and status bar use wide letter-spacing.

**Deep space observatory (`observatory`)** — calm indigo.
- Background `#0a0820`, panel surface `#1b1840`, deeper surface `#120f33` for the info tile and
  status bar, primary text `#e6e1ff`, secondary/accent `#afa9ec`.
- Tiles: 6 px gaps, 10 px rounded corners, no borders; labels as rounded pills on a translucent
  indigo scrim.
- The info tile and empty slots carry a sparse static star field made with layered
  `radial-gradient` dots (no images, no animation), kept faint enough that text contrast is
  unaffected.
- Offline tile: 2 px pink (`#ed93b1`) inset outline.
- Status bar with rounded top corners.

### 4. Accessibility and legibility (all themes)
- Body-size text has at least 4.5:1 contrast against its background; large clock numerals at
  least 3:1. Focus rings stay clearly visible on every control.
- State is never colour-only: the existing state words stay. Live / connecting / offline remain
  three visibly different colours in each theme (green family, amber family, red or pink family).
- Tile labels are never smaller than in the default theme.
- Honour `prefers-reduced-motion` exactly as today.

### 5. README
A short "Themes" paragraph: where the setting is, that it is per display, the four choices.

## Constraints
- Everything in `docs/specs/ui-redesign.md` section 7 still applies (`textContent` only, strict
  CSP, no new request types, a single `setInterval`, `dailyReload`, rotation and weather logic
  unchanged).
- No flavour text: do not invent labels such as "stardate" or "sol"; themes restyle the real
  clock, date, names and states.
- A dashboard is running from the main checkout on port 1984. Do not stop it, start another on
  that port, or send it write requests.
- This repository is public: placeholder data only.
- Do not commit; leave changes in the working tree.

## Done when
- [ ] `/usr/bin/python3 -m unittest discover -s tests -t .` passes (no Python changed).
- [ ] `git diff --stat HEAD -- app tests www/vendor` is empty.
- [ ] `grep -n "innerHTML\|insertAdjacentHTML\|document.write\|eval(" www/index.html www/dashboard.js` finds nothing; `grep -c setInterval www/dashboard.js` is 1.
- [ ] `grep -n "@import\|url(http\|@font-face\|animation\b\|@keyframes" www/dashboard.css` shows nothing new compared with `git show HEAD:www/dashboard.css`.
- [ ] `node --check www/dashboard.js` passes if `node` is available.
- [ ] Final message lists files changed, the custom properties each theme overrides, and what
      you could not verify.
