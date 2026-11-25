# Codex task spec — Clock and weather over a full-screen camera

Effort: high

## Goal
When one camera tile is shown in browser full screen (its Fullscreen button), a small clock
and weather readout sits in the top-right corner of the video at 50% opacity. It disappears
when the tile leaves full screen.

## Scope — touch ONLY these
- `www/dashboard.js`, `www/dashboard.css`
- `README.md` (one sentence where full screen is described)

No Python, no tests, nothing under `www/vendor/`, no `www/index.html` change unless truly needed.

## What to do
1. **One overlay element per camera tile**, created once when the tile is built (like
   `tile-frame`), class `fullscreen-info`, `aria-hidden="true"`, `pointer-events: none`,
   hidden unless the tile is the full-screen element. Show/hide it with a class or attribute
   that `fullscreenchange` sets on the tile (do not rely only on the `:fullscreen` selector;
   keep it working in the browsers this project supports). Never move, re-insert or rebuild
   the tile, the player or the video for this.
2. **Content**, built with `textContent` / `createElement` only:
   - Time, formatted exactly like the info tile's clock (same options, and the weather
     place's time zone through `displayFormatters` when active, else the computer's).
   - Below it, when weather is enabled and a fresh forecast is available: the condition icon
     (reuse `weatherIcon`), the current temperature (`72°`) and the condition word
     (`conditionFor`). When weather is off, loading or unavailable: the clock only — no error
     text.
3. **Look**: top-right corner, inset from the edges (about 2.5vmin), right-aligned. The whole
   block at `opacity: 0.5`. For legibility over bright video use a flat translucent scrim
   behind the text built from existing colour tokens (rounded corners allowed; no blur, no
   shadows, no glow, no animation). Sizes scale with the viewport: time about
   `clamp(32px, 7vmin, 120px)`, weather line about `clamp(16px, 3vmin, 48px)`, icon matching
   the weather line. Tabular numerals for the time. Must look right in all four themes
   (default, mission, hud, observatory) using their existing custom properties; the theme rule
   that strips decoration from full-screen tiles must not hide this overlay.
   It must not cover the tile's Snapshot/Fullscreen buttons or their focus rings — move the
   overlay below them, or keep the buttons clear, whichever fits the existing layout.
4. **Updates**: refresh the overlay of the full-screen tile from the existing one-second tick
   only when the displayed minute changes, and immediately when entering full screen and when
   a weather response is accepted. No new timers (`grep -c setInterval` stays 1), no new
   requests.
5. Leaving full screen hides the overlay; entering it on another tile shows that tile's.
6. README: one sentence that a full-screen camera shows the time and current weather in the
   top-right corner.

## Constraints
- Everything in `docs/specs/ui-redesign.md` section 7 still applies (strict CSP, `textContent`
  only, single `setInterval`, `dailyReload`, rotation, weather, device and backup logic
  unchanged).
- Do not start, stop or write to any running dashboard. Public repository: placeholder data
  only. Do not commit.

## Done when
- [ ] `/usr/bin/python3 -m unittest discover -s tests -t .` passes (no Python changed).
- [ ] `git diff --stat HEAD -- app tests www/vendor www/index.html` is empty (or index.html
      changes are explained).
- [ ] `grep -n "innerHTML\|insertAdjacentHTML\|document.write\|eval(" www/index.html www/dashboard.js` finds nothing; `grep -c setInterval www/dashboard.js` is 1.
- [ ] `node --check www/dashboard.js` passes.
- [ ] Final message lists files changed and what you could not verify.
