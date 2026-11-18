# Codex task spec — Info tile: large clock, date and weather forecast

Effort: high

## Goal
Add an optional "info tile" to the dashboard grid that shows a large clock, the date, current
weather and a 5-day forecast for a place the user enters in Setup (a US ZIP code or a city name).

## Scope — touch ONLY these
- `app/weather.py` (new), `app/store.py`, `app/server.py`, `app/security.py`, `app/__main__.py`
  (only to create, start and stop the weather service)
- `www/index.html`, `www/dashboard.css`, `www/dashboard.js`
- `tests/test_weather.py` (new — your own tests), `tests/test_store.py`, `tests/test_server.py`
- `README.md`

Do NOT modify `tests/test_security.py`, `tests/test_weather_acceptance.py`, `app/go2rtc.py`,
`app/discovery.py`, `www/vendor/*`, `start.sh`, `scripts/*`.

## Acceptance tests (written by Claude — do NOT modify)
- `tests/test_weather_acceptance.py` — `/usr/bin/python3 -m unittest tests.test_weather_acceptance -v`;
  all must pass. Do not edit, skip, or weaken these tests; if one looks wrong, stop and say why
  in your final message instead of changing it. Its docstring and cases are the contract for the
  two new routes in `check_request` and for `WeatherError`, `validate_settings`,
  `build_geocode_url`, `parse_geocode`, `build_forecast_url`, `parse_forecast`.
- `tests/test_security.py` must also keep passing unchanged.

## What to do

### 1. Weather source (server side only)
The browser never contacts a weather service (CSP stays `connect-src 'self' …`). Python fetches
from Open-Meteo (no API key), verified response shapes:
- Place search: `https://geocoding-api.open-meteo.com/v1/search?name=<place>&count=1&language=en&format=json`
  (+ `countryCode=US` when the place is exactly five digits). Reply
  `{"results":[{"name","latitude","longitude","admin1","country",…}]}`; no `results` key when
  nothing matches.
- Forecast: `https://api.open-meteo.com/v1/forecast?latitude=<2dp>&longitude=<2dp>&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m,is_day&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max&temperature_unit=fahrenheit|celsius&wind_speed_unit=mph|kmh&timezone=auto&forecast_days=5`.
  Errors come back as `{"error":true,"reason":"…"}`.

`app/weather.py`:
- Pure functions per the acceptance tests. `WeatherError(code, message)`; codes used:
  `invalid_settings`, `invalid_place`, `invalid_units`, `place_not_found`, `weather_unavailable`.
  Messages are fixed strings; never include upstream text. `parse_forecast` accepts
  temperatures −130..160 and wind 0..300 (limits fit both unit systems), codes 0..99.
- `fetch_json(url, opener=None)`: `urllib.request` with default certificate verification, the
  URL host must be one of the two hosts above and scheme `https` (assert before opening), no
  redirects followed to other hosts (install a redirect handler that refuses), timeout 10 s,
  read at most 256 KB, `json.loads`; any failure → `WeatherError('weather_unavailable', …)`.
  Send `User-Agent: kasa-camera-dashboard` and nothing else identifying.
- `class WeatherService(store, fetch=fetch_json, clock=time.time)`:
  - `snapshot()` → the dict returned by `GET /api/weather` (below). Never blocks on the network.
  - `configure(body)` → validates, and when `enabled` with a place different from the stored
    one **or with no stored coordinates**, geocodes it (network, ≤10 s) and saves
    `{enabled, place, units, name, lat, lon}` through the store; only when the place is unchanged
    AND coordinates are stored does it skip the network call. Saving a different place while
    disabled stores the place and clears `name`/`lat`/`lon` (no network), so a later enable
    geocodes it. Any change of place or units drops the cached forecast immediately, and a
    fetch that started under older settings must discard its result (use a generation counter).
    Then triggers a refresh. Raises `WeatherError`.
  - A daemon thread refreshes the forecast when enabled: immediately after start/configure, then
    every 15 minutes; after a failure retry in 2 minutes. Keep the last good forecast and its
    fetch time; a forecast older than 3 hours is dropped (tile shows "Weather unavailable").
  - `stop()` ends the thread promptly (use an `Event`); wire it into server shutdown.
  - Thread-safe; must not hold any lock used by camera mutations while on the network.

### 2. Storage (`app/store.py`)
Add an optional `"weather"` object to the state file:
`{"enabled": bool, "place": str, "units": "f"|"c", "name": str, "lat": number, "lon": number}`.
- Missing key → defaults `{"enabled": false, "place": "", "units": "f", "name": "", "lat": null, "lon": null}`.
  A file without the key must load exactly as today (existing installs).
- Validate on load like the rest of the file (types, lat −90..90, lon −180..180 or both null,
  `enabled` true requires a name and coordinates); an invalid weather object alone must **not**
  discard the camera list: reset just the weather object to defaults and log one warning.
- Add `get_weather()` / `set_weather(dict)` using the existing atomic-write path and lock.
- Camera add/rename/remove and layout saves must preserve the weather object (test this).

### 3. API (`app/server.py`, `app/security.py`)
- `GET /api/weather` → 200
  `{"enabled": bool, "place": str, "units": "f"|"c", "name": str,
    "forecast": null | {"current": {...}, "days": [...], "updated": <unix seconds int>},
    "status": "off" | "loading" | "ok" | "unavailable"}`
  (`forecast` is the `parse_forecast` shape plus `updated`). Never includes lat/lon.
- `PUT /api/weather` with JSON `{"enabled": bool, "place": str, "units": "f"|"c"}` → 200 with
  the same shape as GET (status usually `loading`). Errors use the existing
  `{"error": code, "message": text}` format: `invalid_*` → 400, `place_not_found` → 404,
  `weather_unavailable` → 502. Body limit and JSON parsing as the existing PUT `/api/layout`.
  Geocoding makes this request take up to ~10 s; it must not block camera mutations or video.
- `app/security.py`: add exactly these two routes with the same rules as `GET /api/cameras`
  and `PUT /api/layout`. No query strings, no sub-paths.

### 4. Info tile (frontend)
- **Setup:** new section "Clock and weather" (after "Spotlight rotation"): a checkbox "Show the
  info tile", a text field "ZIP code or city" (`id="weather-place"`, max 80, placeholder
  `90210 or Chicago`), a units select (`°F, mph` / `°C, km/h`), and a "Save" button. While
  saving: disable the fields and show "Looking up the place…". On success show the resolved
  name ("Showing weather for Beverly Hills, California, United States.") so the user can
  confirm the match; on error show the server's `message` and keep the fields as typed.
  Load current values from `GET api/weather` when Setup opens.
- **Placement:** when enabled, the info tile takes the **last slot of every page**:
  - Fixed layouts: cameras per page = `columns × rows − 1`; the tile sits in the last cell.
  - Spotlight: the strip shows 3 cameras per page and the tile sits in the 4th row; it is
    never the large tile, has no swap button, and rotation never selects it. With one camera
    and the tile enabled, the camera is large and the tile is alone in the strip.
  - Auto: appended after the last camera.
  - No cameras at all: the existing empty state is shown (no tile).
  Page counts, paging, the health summary and rotation candidates must all use the reduced
  camera-slot count. The tile is one persistent DOM node (never recreated on page change).
- **Content** (all text via `textContent`; scale with the tile using container-relative units
  such as `cqh`/`cqw` with `clamp()`, so it is readable in a 480×258 strip slot and in a
  960×540 grid cell):
  - Clock as the largest element (hours and minutes in the browser's locale format via
    `toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'})`, tabular numerals, updates from the existing
    1 s tick, only touching the DOM when the minute changes), date below it
    (`Wednesday, November 18` via `toLocaleDateString`).
  - Current: temperature (`72°`), a condition word, "Feels 70° · Wind 8 mph", and the resolved
    place name (small, ellipsised).
  - Forecast row: up to 5 days — weekday (`Today` only when the day's date equals the browser's
    local date; days before today are hidden; otherwise `Thu`, …), icon, `high° / low°`, and rain
    chance when ≥ 20 % (`60%`). In a small tile show only as many days as fit (hide overflow
    days with a container query rather than shrinking text below ~12 px).
  - Icons: a small fixed set (clear, partly cloudy, cloudy, fog, drizzle, rain, snow, showers,
    thunderstorm) built as inline SVG through DOM APIs (`createElementNS`), chosen from the WMO
    `code` (0 clear; 1–2 partly; 3 cloudy; 45,48 fog; 51–57 drizzle; 61–67 rain; 71–77 snow;
    80–82 showers; 85–86 snow; 95–99 thunderstorm; anything else cloudy). Use `is_day` to swap
    sun for moon on clear/partly. Each icon has a text alternative (the condition word).
  - States: `loading` → clock and date plus "Loading weather…"; `unavailable` or a forecast
    whose `updated` is more than 3 hours old → clock and date plus "Weather unavailable"; the
    clock and date always render, even when `api/weather` fails.
- Enabling, disabling or refreshing the tile must not detach or re-insert any camera tile, and
  saving weather settings must not restart go2rtc or interrupt video.
- **Refresh:** fetch `api/weather` on load, after a successful save, and every 5 minutes from the
  existing 1 s tick (no new timers); after a `loading` reply, poll every 5 s until it settles
  (max 1 minute). Failures are silent retries at the next 5-minute mark.
- The status bar keeps its small clock and date unchanged.

### 5. README
Document the info tile, the Setup fields, and a short privacy note: the place you type and its
coordinates rounded to two decimals (about 1 km) are sent from this computer to Open-Meteo
(`open-meteo.com`) to look up the forecast; nothing else is sent, and the browser itself makes
no external requests. Add the two routes to the API table. Add the credit line "Weather data
by Open-Meteo.com (CC BY 4.0)" and note that Open-Meteo's free service is for non-commercial use.

## Constraints
- Python 3.9 standard library only; plain ES modules; no new dependencies; no new files beyond
  the two listed.
- Outbound requests go only to the two Open-Meteo hosts, only from `app/weather.py`, and only
  while the info tile is enabled or a place is being saved.
- Everything in `docs/specs/ui-redesign.md` section 7 still applies (strict CSP unchanged,
  `textContent` only, `dailyReload` and rotation unchanged, a single `setInterval`).
- Your tests use a fake `fetch` and fake clock: no real network, no sleeping, no real `.env`.
- This repository is public: placeholder data only in tests and docs (`Springfield`, `90210`).
- A dashboard is running from the main checkout on port 1984. Do not stop it, start another on
  that port, or send it write requests.
- Do not commit; leave changes in the working tree.

## Done when
- [ ] `/usr/bin/python3 -m unittest discover -s tests -t .` passes.
- [ ] `git diff --stat HEAD -- tests/test_security.py tests/test_weather_acceptance.py app/go2rtc.py www/vendor` is empty.
- [ ] `grep -n "innerHTML\|insertAdjacentHTML\|document.write\|eval(" www/index.html www/dashboard.js` finds nothing; `grep -c setInterval www/dashboard.js` is 1.
- [ ] `grep -rn "open-meteo" app www | grep -v "app/weather.py"` finds nothing.
- [ ] `node --check www/dashboard.js` passes if `node` is available.
- [ ] Final message lists files changed and what you could not verify.

## Context Codex needs
- `renderPage()` in `www/dashboard.js` computes slots/pages/visible tiles for fixed layouts and
  Spotlight; `rotateSpotlight()` picks from `state.visible`; `updateSummary()` builds the health
  text. The Spotlight strip is 4 rows placed by CSS classes (`spotlight-large`,
  `spotlight-strip`) on children of the single grid container.
- `app/store.py:validate_state` currently raises on any invalid state and the caller renames
  the file as corrupt; the weather object needs the softer handling described above.
- `app/server.py` serialises camera mutations with a lock and has `refresh_once` on a 60 s
  cycle; do not run weather network calls under that lock.
