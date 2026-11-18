# Codex task spec — Orientation-aware layouts (landscape and portrait displays)

Effort: high

## Goal
Make every layout, the info tile, the status bar and all themes work well on a display mounted
vertically (portrait, e.g. 1080×1920) as well as horizontally, switching automatically when the
viewport's orientation changes.

## Scope — touch ONLY these
- `www/index.html`, `www/dashboard.css`, `www/dashboard.js`
- `README.md` ("Using the dashboard": a short "Portrait displays" paragraph)

No Python changes, no test changes, no new files, nothing under `www/vendor/`.

## What to do

### 1. Orientation
- Portrait means the single media query `(orientation: portrait) and (min-width: 700px)`
  matches (below 700 px the existing narrow-screen Auto fallback stays as it is). Use that one
  query in both CSS and `matchMedia`, so one `change` event covers both conditions.
- Check the existing `max-width: 899px` rules: they must still give a sensible result on a
  narrow portrait monitor such as 768×1366 (no overlap, no horizontal scrolling).
- Expose it as `data-orientation="portrait" | "landscape"` on `.dashboard`; update on the media
  query's `change` event and re-run `renderPage()`. No resize polling, no new timers.
- Landscape behaviour must stay **exactly** as it is today: same grids, slots, pages, sizes.
- The saved layout value (`2x1`, `2x2`, `3x3`, `4x4`, `spotlight`, `auto`) and its button labels
  do not change with orientation; only the geometry used to draw it does. One display can be
  portrait and another landscape with the same saved layout.

### 2. Geometry in portrait
Camera pictures are 16:9, so portrait grids use fewer columns and more rows. One function must be
the single source of truth for `{columns, rows}` given `(layout, orientation)`; slot counts, page
counts, the health summary, rotation candidates and the info tile's cell must all derive from it
(replace the `layout.split('x')` arithmetic).

| Saved layout | Landscape (unchanged) | Portrait |
| --- | --- | --- |
| `2x1` | 2 columns × 1 row | 1 column × 2 rows |
| `2x2` | 2 × 2 | 1 column × 4 rows |
| `3x3` | 3 × 3 (9 slots) | 2 columns × 5 rows (10 slots) |
| `4x4` | 4 × 4 | 2 columns × 8 rows |
| `auto` 1 / 2 / 3–4 tiles | single / pair / quad | 1 column × that many rows |
| `auto` 5–6 tiles | 3 × 2 | 2 × 3 |
| `auto` 7–9 tiles | 3 × 3 | 2 × 5 |
| `auto` 10 tiles | existing scrolling grid | 2 × 5 |
| `auto` more | existing scrolling grid | existing scrolling grid |

- The info tile still takes the **last slot of every page** (last row, last column).
- Empty slots, 2 px gaps (or the theme's gap) and "no page scrolling" apply as in landscape.

### 3. Spotlight in portrait
- The large tile spans the full width at the top; the strip becomes a **2 × 2 block below it**
  (4 slots, same slot count and paging as the landscape strip, filled left-to-right then
  top-to-bottom; the info tile, when enabled, is the 4th slot = bottom-right).
- Row heights: large tile `2fr`, each strip row `1.5fr` (on 1080×1920 that gives a large tile
  about 1080×750 and strip cells about 540×560).
- With one camera and no info tile the large tile fills the whole area, as today.
- Swap, rotation, paging and "strip tiles have exactly one control" are unchanged. Keep placement
  CSS-driven (classes / custom properties on existing nodes): **no tile node is moved, removed
  or re-inserted** when orientation changes.

### 4. Info tile shapes
The tile already adapts with container queries. Shapes follow the **cell**, not the display
orientation. Add one shape:
- **Wide and short** (container aspect ratio ≥ 2.2, e.g. a 1080×470 cell): two columns — clock
  and date on the left, current weather and forecast on the right — so nothing is clipped and
  text is not shrunk to fit the height. Aspect-ratio container queries need
  `container-type: size` on the info container; set it if it is not already.
No landscape cell at 1920×1080 or 1920×1200 reaches that ratio, so landscape rendering is
unchanged. Do not add a separate "tall" shape.
Nothing may be clipped in any cell produced by section 2 or 3 at 1080×1920 and 1920×1080; hide
lower-priority items (place name, then later forecast days) rather than overflow.

### 5. Status bar and Setup in portrait
- At 1080 px wide the always-visible group (clock, date, health, notices) stays on one row.
  When the page is active and the controls do not fit beside it, they wrap onto a second row
  (the bar grows; the camera area shrinks accordingly — no overlap, no horizontal scrolling).
  When idle the hidden controls must leave the flow in portrait (not just become invisible), so
  the bar returns to one row and the camera area grows back.
- The Setup sheet keeps `min(420px, 100vw)` width and full height.

### 6. Themes
All four looks (default, `mission`, `hud`, `observatory`) must work in portrait with the same
rules as landscape: frames, brackets and cut corners follow the new tile shapes; nothing covers
the picture; labels and buttons stay clear of HUD corner cuts.

## Constraints
- Everything in `docs/specs/ui-redesign.md` section 7 and `docs/specs/space-themes.md`
  section 2 still applies (`textContent` only, strict CSP, single `setInterval`, no animations,
  `dailyReload`/rotation/weather logic unchanged apart from reading slots from the new function).
- An orientation change must not interrupt video on tiles that stay visible. Tiles that move to
  or from a hidden page connect/disconnect exactly as with a normal page change.
- A dashboard is running from this worktree on port 1984 and serves these files live. Do not
  stop it, start another, or send it write requests.
- This repository is public: placeholder data only.
- Do not commit; leave changes in the working tree.

## Done when
- [ ] `/usr/bin/python3 -m unittest discover -s tests -t .` passes (no Python changed).
- [ ] `git diff --stat HEAD -- app tests www/vendor` is empty.
- [ ] `grep -n "innerHTML\|insertAdjacentHTML\|document.write\|eval(" www/index.html www/dashboard.js` finds nothing; `grep -c setInterval www/dashboard.js` is 1.
- [ ] `grep -n "layout.split" www/dashboard.js` finds nothing outside the new geometry function.
- [ ] `node --check www/dashboard.js` passes if `node` is available.
- [ ] Final message lists files changed and what you could not verify.
