# Codex task spec — Choose which cameras take part in rotation

Effort: high

## Goal
Each camera row in Setup gets a checkbox "In rotation". Only ticked cameras are used by the
rotation timer (Setup → Rotation). All cameras are ticked by default.

## Context (already on this branch)
`rotateSpotlight()` in `www/dashboard.js` runs from the single one-second tick. In Spotlight it
makes the next Live camera on the visible strip page large; in other layouts with more than one
page it now shows the next page (wrapping), and `changePage()` restarts the interval. The
rotation interval is stored per display in `localStorage['kcd.rotate']`.

## Scope — touch ONLY these
- `www/dashboard.js`, `www/dashboard.css` (only if the row needs layout help), `www/index.html`
  (only if a help line is needed), `README.md` (rotation paragraph)

No Python, no tests, nothing under `www/vendor/`.

## What to do
1. **Storage, per display** (like `kcd.rotate`): `localStorage['kcd.rotation-skip']` = JSON array
   of camera ids left out of rotation. Missing/invalid → empty (everyone in). Read with the
   existing try/catch pattern; ignore ids that are not current cameras; when a camera is
   removed, drop its id. A newly added camera is in rotation (not in the skip list).
2. **Setup camera rows**: add a checkbox labelled "In rotation" to each row of the existing camera
   list (with a proper `<label>` / `aria-label` naming the camera, e.g. "Front in rotation").
   Changing it saves immediately, announces "Front added to rotation." / "Front left out of
   rotation." in `#setup-announcement`, and restarts the rotation interval. Rows are rebuilt by
   the existing code: keep the checkbox state in sync when rows are rebuilt and do not steal
   focus. Add one help sentence near the camera list or the Rotation section: "Untick a camera
   to leave it out of rotation on this display."
3. **Rotation rules**:
   - Spotlight: the next large camera is the next camera that is ticked, Live and on the visible
     strip page (today's rules plus "ticked"). If none qualifies, nothing changes.
   - Paged layouts: advance to the next page (wrapping) that contains at least one ticked
     camera; skip pages with none. If no page other than the current one qualifies, stay.
   - If no camera is ticked, rotation does nothing (the timer setting itself is unchanged).
   - Manual selection and manual paging still reach every camera, ticked or not.
4. **README**: one or two sentences in the rotation paragraph about the per-camera tick box.

## Constraints
- Everything in `docs/specs/ui-redesign.md` section 7 still applies (strict CSP, `textContent`
  only, single `setInterval`, `dailyReload`). No new timers or requests.
- Do not start, stop or write to any running dashboard. Public repository: placeholder data
  only. Do not commit.

## Done when
- [ ] `/usr/bin/python3 -m unittest discover -s tests -t .` passes.
- [ ] `git diff --stat HEAD -- app tests www/vendor` is empty.
- [ ] `grep -n "innerHTML\|insertAdjacentHTML\|document.write\|eval(" www/index.html www/dashboard.js` finds nothing; `grep -c setInterval www/dashboard.js` is 1.
- [ ] `node --check www/dashboard.js` passes.
