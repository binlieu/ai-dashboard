# Codex task spec — Clock follows the weather place's time zone

Effort: high

## Goal
When the info tile is on and a forecast has been received, the clock and date (status bar and
info tile) show the time at the weather place (the ZIP code / city entered in Setup) instead of
the computer's own time zone. Otherwise they show the computer's time, exactly as today.

## Scope — touch ONLY these
- `app/weather.py`
- `tests/test_weather.py` (only to adapt assertions to the new `timezone` key and add cases)
- `www/dashboard.js`
- `www/index.html` (only if a help-text line is added, see step 6)
- `README.md` (weather section and the `/api/weather` row)

Do NOT modify anything else: no other Python, no CSS, nothing under `www/vendor/`, no store
changes (the zone is **not** persisted in `data/cameras.json`).

## Acceptance tests (written by Claude — do NOT modify)
- `tests/test_timezone_acceptance.py` — `/usr/bin/python3 -m unittest tests.test_timezone_acceptance`;
  all must pass. Also do not modify `tests/test_weather_acceptance.py` or `tests/test_security.py`.
  If one looks wrong, stop and say why in your final message instead of changing it.

## What to do

### Server (`app/weather.py`)
1. Add `parse_timezone(payload)`: returns the `timezone` value of a forecast response when it is
   a `str` of 1–64 characters fully matching
   `[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*` (ASCII only); otherwise `None`. It never raises, whatever
   the input type. `parse_forecast` keeps returning exactly today's shape (no new key).
2. `WeatherService` keeps the zone in memory, separate from the forecast cache:
   - set from a successful forecast fetch (same generation check as the forecast) when
     `parse_timezone` gives a name; a successful fetch with no usable name keeps the last one;
   - kept when a fetch fails, when the cached forecast expires after 3 h, and when only the
     units change or the tile is switched off and on again for the same place;
   - cleared when the saved place changes.
   The request already sends `timezone=auto`; do not change `build_forecast_url`.
3. `snapshot()` (and so `GET`/`PUT /api/weather`) gains a top-level `"timezone"`: the zone name
   while weather is enabled and a zone is known, else `None`. No other key changes; coordinates
   stay private.

### Page (`www/dashboard.js`)
4. One helper owns "what time is it on this display": given a `Date`, it returns the parts needed
   (year, month, day, hour, minute as numbers) for the active zone, using `Intl.DateTimeFormat`
   with `timeZone` and `formatToParts` (`hourCycle: 'h23'`). The active zone is
   `weatherData.timezone` when `weatherData.enabled` is true and the value is a string the
   browser accepts; validate once per distinct value by constructing a formatter in `try/catch`
   and cache the formatter objects (do not construct formatters every second). No zone, weather
   off, or a rejected name → the computer's zone, with output identical to today's.
   `acceptWeather` must accept a missing or `null` `timezone` (older server) without error.
   **Displayed text keeps today's formatting calls**: the same `toLocaleTimeString` /
   `toLocaleDateString` options as now, with `timeZone` added when a zone is active (or cached
   `Intl.DateTimeFormat` objects with exactly those options). Never assemble displayed text
   from the numeric parts; the parts are only for the minute/day change keys, the "today"
   comparison and the `datetime` date value.
5. Use the active zone for:
   - `updateClock()` — status-bar time and date text, and the `<time datetime>` date value;
   - `weatherTick()` — info-tile clock and date, and the minute/day change keys (`infoMinute`,
     `infoDay`), so the tile re-renders when the *place's* minute or day rolls over;
   - `renderWeather()` — "today" is the place's date, so the right forecast day is labelled
     "Today" and past days are dropped by the place's calendar. Weekday labels for forecast
     days keep the existing noon-anchored formatting **without** `timeZone` (they are plain
     calendar dates; formatting the computer's noon in a far-away zone gives the wrong weekday).
   - When a weather response changes the active zone, refresh both clocks, the date and the
     forecast row immediately rather than waiting for the next minute.
   Keep the computer's own time for: `dailyReload()` (still 04:00–05:00 on the computer),
   snapshot file names, and every freshness/polling calculation (those use epoch time already).
   `datetime` attributes that hold a full instant keep `toISOString()`.
6. Setup: under the place input in "Clock and weather", make it clear the clock follows the
   place. Prefer extending the existing help text there with one short sentence, e.g.
   "The clock shows this place's local time." Do not add a control.

### Docs
7. README: one or two sentences in the weather section (clock follows the place's time zone once
   a forecast has arrived; computer time when the tile is off or no forecast has been received
   since the server started), and add `timezone` to the `/api/weather` description.

## Constraints
- Everything in `docs/specs/ui-redesign.md` section 7 still applies: `textContent` only, strict
  CSP, no new request types, a single `setInterval`, no new dependencies, Python 3.9 stdlib only.
- The zone name reaches the page only as data passed to `Intl.DateTimeFormat`; never write it
  into the DOM.
- Do not change time formats: status bar stays 24-hour `HH:MM`, the info tile keeps
  `hour: 'numeric', minute: '2-digit'` and the same date styles, in the browser's locale.
- A dashboard is running from the main checkout on port 1984. Do not stop it, start another on
  that port, or send it write requests.
- This repository is public: placeholder data only (no real places beyond the ones already used
  in the tests).
- Do not commit; leave changes in the working tree.

## Done when
- [ ] `/usr/bin/python3 -m unittest discover -s tests -t .` passes, including the new acceptance file.
- [ ] `git diff --stat HEAD -- tests/test_timezone_acceptance.py tests/test_weather_acceptance.py tests/test_security.py app/store.py www/vendor www/dashboard.css` is empty.
- [ ] `grep -n "innerHTML\|insertAdjacentHTML\|document.write\|eval(" www/index.html www/dashboard.js` finds nothing; `grep -c setInterval www/dashboard.js` is 1.
- [ ] `node --check www/dashboard.js` passes if `node` is available.
- [ ] Final message lists files changed and what you could not verify.

## Context Codex needs
- Open-Meteo's forecast response carries a top-level `"timezone": "America/Chicago"` when the
  request has `timezone=auto`; `refresh_once()` already has the raw payload before
  `parse_forecast`.
- `tests/test_weather.py::test_default_snapshot_never_fetches_and_does_not_expose_coordinates`
  compares the whole default snapshot and needs the new `'timezone': None` entry.
- Clock code today: `updateClock()` near the end of `www/dashboard.js`, `weatherTick()`,
  `localDate()` and `renderWeather()` in the weather section.
