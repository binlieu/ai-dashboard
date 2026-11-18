# Codex task spec — Device control 3/3: Setup list and status-bar buttons

Effort: high

## Goal
Setup gets a "Plugs and switches" section (add by MAC, rename, remove), and each saved device
gets an on/off button in the status bar that shows the state the device reports.

## Scope — touch ONLY these
- `www/index.html`, `www/dashboard.css`, `www/dashboard.js`
- `README.md` ("Using the dashboard": a short "Plugs and switches" paragraph)

No Python, no tests, nothing under `www/vendor/`.

## What to do
1. **Setup section "Plugs and switches"** (after the camera list, before "Clock and weather"),
   built like the camera section: list rows (name, MAC, state word, Rename, Remove), an add
   form (`MAC address`, optional `Name`, button "Add device"), a count "n of 6", inline error
   text from the server's `message`, a busy state while adding (adding can take ~10 s), and
   announcements in `#setup-announcement` ("Porch light added.", "… removed."). Help text:
   "Kasa plugs and switches on this network. Anyone using this screen can switch them."
   Remove asks for confirmation the same way cameras do.
2. **Status bar**: a group `#device-controls` (role `group`, `aria-label` "Plugs and switches")
   placed before the layout buttons, one `<button type="button" class="device-button">` per
   device in saved order: text = device name (truncate with ellipsis, max about 14ch, full name
   in `title`), `aria-pressed` true/false for on/off, and a visible state that is not
   colour-only: a leading marker plus the word "On"/"Off" (visually compact, e.g. "Porch · On").
   - Unknown state: `aria-pressed="mixed"`, text "… · ?", and a press only re-reads the state
     (GET) and shows the result; it never sends a command blind.
   - Press: disable that one button (`aria-busy`), `PUT api/devices/<id>/state` with
     `{"on": !current}`, then show the state from the response. On error show the server
     message in the existing notice area for 6 s and mark the button unknown.
   - No optimistic flip: the button changes only when the server answers.
   - The group follows the same idle behaviour as the other status-bar controls (hidden while
     idle, shown on activity). With no devices the group is absent and nothing else moves.
   - Narrow/portrait bars: the group wraps or scrolls horizontally inside the bar without
     pushing the clock, date or health counts off-screen; with 6 devices at 1280 px wide
     nothing overlaps.
3. **Data flow**: `GET api/devices` on load, after every Setup change, and every 15 s from the
   existing single one-second tick (no new timers, `grep -c setInterval` stays 1). Skip the
   poll while a state change for that device is in flight. Build DOM with `textContent` only;
   update buttons in place (do not rebuild the group every poll, so focus is kept).
4. **Themes**: style the group in all four themes (default, mission, hud, observatory) using
   the existing custom properties; "on" uses the theme's live/green family, "off" the neutral
   control style, unknown the connecting/amber family. Focus rings visible. No new colours
   outside custom properties, no animations, no glows.
5. Keyboard: buttons are normal tab stops; the arrow-key Spotlight handler must ignore them
   (it already ignores interactive elements — verify).

## Constraints
- Everything in `docs/specs/ui-redesign.md` section 7 still applies (strict CSP, `textContent`
  only, single `setInterval`, `dailyReload`, rotation and weather logic unchanged).
- The API (task 2): `GET /api/devices` → `{"devices":[{"id","name","mac","state"}],"max":6}`;
  `POST /api/devices` `{mac,name}` → 201 device; `PATCH /api/devices/<id>` `{name}`;
  `DELETE` → 204; `PUT /api/devices/<id>/state` `{"on":bool}` → `{"id","name","state"}`;
  errors `{error,message}`. A server without `/api/devices` (404) must leave the page working
  with the feature absent.
- A dashboard is running on port 1984: do not stop it or send it write requests. Never switch a
  real device while working. Public repository: placeholder data only. Do not commit.

## Done when
- [ ] `/usr/bin/python3 -m unittest discover -s tests -t .` passes (no Python changed).
- [ ] `git diff --stat HEAD -- app tests www/vendor` is empty.
- [ ] `grep -n "innerHTML\|insertAdjacentHTML\|document.write\|eval(" www/index.html www/dashboard.js` finds nothing; `grep -c setInterval www/dashboard.js` is 1.
- [ ] `node --check www/dashboard.js` passes if `node` is available.
- [ ] Final message lists files changed and what you could not verify.
