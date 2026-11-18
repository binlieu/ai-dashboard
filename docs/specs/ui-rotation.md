# Codex task spec — Spotlight rotation timer (follow-up to ui-redesign)

Effort: medium

## Goal
Add a "Spotlight rotation" setting to the Setup sheet that automatically changes which camera is
large in the Spotlight layout every N seconds.

## Scope — touch ONLY these
- `www/index.html`, `www/dashboard.js`, `www/dashboard.css`
- `README.md` ("Using the dashboard": two or three sentences)

No Python changes, no new files, no changes to `tests/*` or `www/vendor/*`.

## What to do
1. **Setting.** In the Setup dialog add a third section, after "Cameras", titled "Spotlight
   rotation": a `<label for="rotation-interval">Change the large camera</label>` and a
   `<select id="rotation-interval">` with options `Off` (value `0`, default), `Every 10 seconds`
   (`10`), `Every 30 seconds` (`30`), `Every minute` (`60`), `Every 5 minutes` (`300`), plus one
   line of help text: "Applies to the Spotlight layout on this display." Style it like the other
   Setup fields (min 40 px tall).
2. **Storage.** Persist the value in `localStorage['kcd.rotate']` (same try/catch pattern as
   `kcd.spotlight`). On load, accept only the five listed values; anything else means `0`.
   Changing the select applies immediately and announces "Rotation set to <option text>." (or
   "Rotation off.") in the existing `#setup-announcement` status region.
3. **Rotation.** Drive it from the existing 1 s interval (no new timers): keep a
   `nextRotationAt` timestamp. When all of these hold — interval > 0, effective layout is
   `spotlight`, the tab is visible, the Setup dialog is closed, no element is fullscreen, and
   `Date.now() >= nextRotationAt` — pick the next camera and make it the spotlight camera through
   the same code path as a manual swap, except:
   - do **not** call `activate()` (rotation must not wake the controls on an idle display), and
   - do **not** move keyboard focus.
   Refactor `selectSpotlight` so both callers share the state/storage/render part.
4. **Which camera is next.** Candidates are the cameras currently shown in the strip (the
   visible strip page) whose tile is in the Live state, in configured order, starting after the
   current spotlight camera and wrapping around. If there is no Live candidate, do nothing and
   try again at the next interval. Cameras on a hidden strip page are never chosen (they hold no
   connection, so they would appear large while still connecting).
5. **Timer resets.** Set `nextRotationAt = Date.now() + interval` whenever: the interval
   changes, the layout becomes Spotlight, a manual swap happens, the tab becomes visible again,
   the Setup dialog closes, or a rotation fires.
6. The rotated camera id is saved to `kcd.spotlight` like a manual choice, so the daily reload
   resumes from where it was.

## Constraints
- Everything in the regression list of `docs/specs/ui-redesign.md` section 7 still applies
  (`textContent` only, strict CSP, no new request types, `dailyReload` unchanged).
- A swap must stay a class change: no tile node is removed or re-inserted.
- A dashboard is running from this worktree on port 1984 and serves these files live. Do not
  stop it, start another, or send it write requests.
- Do not commit; leave changes in the working tree.

## Done when
- [ ] `/usr/bin/python3 -m unittest discover -s tests -t .` passes (no Python changed).
- [ ] `git diff --stat HEAD -- app tests www/vendor` is empty.
- [ ] `grep -n "innerHTML\|insertAdjacentHTML\|document.write\|eval(\|setInterval" www/dashboard.js`
      shows no `innerHTML`-family hits and exactly the one existing `setInterval`.
- [ ] `node --check www/dashboard.js` passes if `node` is available.
- [ ] Final message lists files changed and what you could not verify.
