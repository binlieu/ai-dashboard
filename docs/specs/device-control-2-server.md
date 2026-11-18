# Codex task spec — Device control 2/3: store, service and API

Effort: high

## Goal
Plugs and switches can be saved by MAC address and switched through the dashboard's own API,
using `DeviceClient` from task 1.

## Scope — touch ONLY these
- `app/security.py`, `app/store.py`, `app/devices.py` (new), `app/server.py`, `app/__main__.py`
- `tests/test_store.py`, `tests/test_server.py`, `tests/test_devices.py` (new)
- `README.md` (API table and a short "Plugs and switches" paragraph incl. privacy/safety note)

No page files in this task. Do not change `app/aes.py`, `app/kasa_control.py`, `app/identify.py`.

## Acceptance tests (written by Claude — do NOT modify)
- `tests/test_security_devices_acceptance.py` and every other `tests/*_acceptance.py`, plus
  `tests/test_security.py`. All must pass: `/usr/bin/python3 -m unittest discover -s tests -t .`

## What to do
1. **Gate** (`app/security.py`): `DEVICE_ID = dev[1-9][0-9]{0,3}`. Allow `GET, POST /api/devices`;
   `PATCH, DELETE /api/devices/<id>`; `PUT /api/devices/<id>/state`. Same rules as the camera
   routes (no query string, same-origin for writes, JSON for POST/PATCH/PUT).
2. **Store** (`app/store.py`): optional top-level `devices` (list) and `next_device_id` (int ≥ 1)
   in `data/cameras.json`; files without them stay valid and are not rewritten on load.
   Each device is exactly `{id, mac, name}`; ids `devN` below `next_device_id`, unique; MAC
   normalised and unique among devices **and not used by any camera** (and a camera cannot be
   added with a device's MAC); names use the existing `validate_name`. `MAX_DEVICES = 6`.
   Invalid device data on load resets devices only (warn, keep cameras) — same idea as weather.
   Methods: `get_devices()`, `check_add_device(mac, name)`, `add_device`, `rename_device`,
   `remove_device`; errors as `StoreError` with codes `invalid_mac`, `invalid_name`,
   `duplicate` (409), `limit` (409), `not_found` (404). Camera rollback (`restore`) must not
   drop a device saved meanwhile (same pattern as weather).
3. **Service** (`app/devices.py`): `DeviceService(store, client, lookup=ip_for_mac,
   sweep=sweep_local_network, identify=identify_device, clock=time.monotonic)` with its own
   lock; it never takes the camera application lock and never holds its lock during network I/O
   longer than one device operation (serialise operations per service; reads from `snapshot()`
   must not wait on the network).
   - `snapshot()` → `{"devices": [{"id","name","state"}], "max": 6}` with state `"on"`, `"off"`
     or `"unknown"`. Never returns MAC-derived IPs; MACs are returned only by `settings()`
     (`{"devices":[{"id","name","mac","state"}], "max": 6}`) — use `settings()` for `GET /api/devices`
     because Setup lists MACs exactly as it does for cameras.
   - `add(body)`: validate → find the IP (lookup, one sweep, lookup) else 404 `not_found`
     "Device not found on this network." → `identify(ip)`: kind must be `plug`, `switch` or
     `plug_or_switch`; a camera → 422 `not_device` "This is a camera. Add it under Cameras.";
     anything else or unknown → 422 `not_device` with "This is not a Kasa plug or switch." (when
     the kind is known, reuse the device words from `describe`-style naming, e.g. "This is a
     TP-Link Wi-Fi router (X55), not a plug or switch.") → `client.read(ip)` must succeed
     (`auth` → 422 `auth` "The device rejected the Kasa account login."; `unsupported` → 422
     `not_device` "Multi-outlet devices are not supported yet."; else 502 `unreachable`
     "The device did not answer.") → save, return the device with its state (201).
   - `set_state(device_id, body)`: body must be exactly `{"on": true|false}` (strict bool) else
     400 `invalid_state`. Resolve the IP (cached; refresh via lookup when missing or after a
     failure), `client.set`, update the cached state, return `{"id","name","state"}`.
     Failures → 502 `unreachable` (or 422 `auth`) and the cached state becomes `"unknown"`.
   - Background thread (`start()`/`stop()` like `WeatherService`): every 15 s read each device's
     state (one at a time); a failure marks it `"unknown"` and retries next round; refresh a
     missing IP with `lookup` (no sweeps in the background).
   - `rename`, `remove`.
4. **Server** (`app/server.py`, `app/__main__.py`): routes map to the service; `DeviceClient`
   is built in `__main__` from the Kasa login already loaded from `.env` (the app keeps the
   password base64-encoded for cameras; decode it in memory for the client, never log it, never
   put it in `run/go2rtc.yaml` or any file). `DashboardApplication` takes `devices=None`
   (tests inject a fake). Errors use the existing `{ "error", "message" }` shape. Start/stop
   the service with the application. Device requests must not block camera requests.

## Constraints
- Python 3.9 stdlib only; match existing style; tests use fakes only (no real network).
- Never log or return credentials; state-changing routes exist only as listed above.
- A dashboard is running on port 1984: do not stop it, start another on that port, or send it
  write requests. Public repository: placeholder data only. Do not commit.

## Done when
- [ ] `/usr/bin/python3 -m unittest discover -s tests -t .` passes completely.
- [ ] `git diff --stat HEAD -- tests/test_security.py 'tests/*_acceptance.py' www app/aes.py app/kasa_control.py app/identify.py` is empty.
- [ ] Final message lists files changed and what you could not verify.
