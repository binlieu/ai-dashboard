# Codex task spec — Name the device when an added MAC address is not a camera

Effort: high

## Goal
When someone adds a MAC address in Setup and the device is not a camera, the error says what it
is ("This is a Kasa smart switch (HS200), not a camera.") instead of the generic message. The
server learns this by asking that one device over the two local Kasa discovery protocols.

## Scope — touch ONLY these
- `app/identify.py` (new)
- `app/server.py` (wire into `Application.add` and the constructor only)
- `app/__main__.py` (only if the constructor wiring needs it)
- `tests/test_identify.py` (new, your own extra cases), `tests/test_server.py`
- `README.md` (Setup section and the camera-API error paragraph)

Do NOT modify anything else: no page files (`www/`), no store, no go2rtc module.

## Acceptance tests (written by Claude — do NOT modify)
- `tests/test_identify_acceptance.py` — `/usr/bin/python3 -m unittest tests.test_identify_acceptance`;
  all must pass. Also do not modify `tests/test_security.py`, `tests/test_weather_acceptance.py`
  or `tests/test_timezone_acceptance.py`. If a test looks wrong, stop and say why in your final
  message instead of changing it.

## What to do

### `app/identify.py` (Python 3.9, standard library only)
1. Constants: `LEGACY_PORT = 9999`, `NEW_PORT = 20002`,
   `LEGACY_QUERY` = the bytes of `{"system":{"get_sysinfo":{}}}` under the Kasa autokey XOR
   (key starts at 171; each output byte is `key ^ plain`, then becomes the key),
   `NEW_QUERY = bytes.fromhex("020000010000000000000000463cb5d3")`.
2. `parse_identity(port, data)` → `{"kind": ..., "model": ...}` or `None`. Never raises, for any
   input types. Datagrams over 8192 bytes are refused.
   - Port 9999: decode the autokey XOR, parse JSON, read `model` and the type (`mic_type` or `type`)
     from `system.get_sysinfo`; Kasa cameras nest the same fields one level deeper, in
     `system.get_sysinfo.system` — use that object when it is a dict.
   - Port 20002: skip the 16-byte header, parse JSON, read `result.device_model` and
     `result.device_type`.
   - Any other port → `None`.
   - **Model**: a string; drop a trailing region suffix like `(US)`; the rest must fully match
     `[A-Za-z0-9][A-Za-z0-9 _.+-]{0,23}`, otherwise the model is `None`.
   - **Kind** (fixed vocabulary: `plug`, `switch`, `plug_or_switch`, `light`, `camera`,
     `router`, `other`), decided in this order:
     1. prefix of the cleaned model (case-insensitive): `HS1`, `HS3`, `KP`, `EP` → `plug`; `HS2`, `KS`, `ES` →
        `switch`; `KL`, `LB` → `light`; `KC`, `EC`, `KD` → `camera`;
     2. otherwise the type string (case-insensitive substring): `CAMERA` → `camera`;
        `PLUGSWITCH` → `plug_or_switch`; `SWITCH` → `switch`; `PLUG` → `plug`; `BULB` or
        `LIGHT` → `light`; `HOMEWIFISYSTEM` or `ROUTER` → `router`;
     3. otherwise `other`.
   - If there is neither a usable model nor a string type, return `None` (for example an
     answer holding only an error code).
   - Return exactly the two keys. Nothing else from the response (alias, device id, owner,
     coordinates, MAC, IP) is kept, logged or returned.
3. `describe(identity)` → the user-facing sentence. The acceptance tests fix the wording:
   Kasa smart plug / smart switch / smart plug or switch / smart light, "TP-Link Wi-Fi router",
   "TP-Link device", the camera sentence, the form without a model, and the unchanged generic
   sentence for `None`.
4. `identify_device(ip, timeout=1.5, socket_factory=None)` → identity or `None`. Never raises.
   - Accept only a unicast IPv4 address in dotted form (not multicast, broadcast, unspecified,
     a host name, or anything with a port); otherwise return `None` without creating a socket.
   - One UDP socket (default factory: `socket.socket(AF_INET, SOCK_DGRAM)`); no broadcast
     option. Send `LEGACY_QUERY` to `(ip, 9999)` and `NEW_QUERY` to `(ip, 20002)`, then wait in two phases
     (UDP may drop): phase one lasts half the timeout; if it ends with no accepted answer, send
     the pair once more and keep listening until the full timeout. A read timeout in phase one
     starts the resend; it does not end the wait.
   - Read until `timeout` seconds have passed in total (monotonic clock; set the socket timeout
     to the remaining time before each read). Use only datagrams whose source is exactly
     `(ip, 9999)` or `(ip, 20002)`; ignore the rest. Return the first identity that
     `parse_identity` accepts. Read timeouts are `socket.timeout` on real sockets and
     `TimeoutError` in the tests' fake socket; catch them **before** the general `OSError`
     (on Python 3.9 both are `OSError` subclasses). A timeout in phase two, or any other
     `OSError` at any point, ends the wait with `None`.
   - Always close the socket.

### `app/server.py`
5. `DashboardApplication.__init__` takes `identify=identify_device` (same injection pattern as `probe`).
   In `add`, **only** where the probe result today raises `not_kasa`, call
   `self.identify(ip)` and raise `StoreError("not_kasa", describe(identity), 422)`. Error code
   and status are unchanged; only the message can differ. The camera path (`ok`/`busy`), the
   `auth` path and the `not_found` path make no identification request. An exception from the
   injected identify function falls back to the generic message.

### Tests and docs
6. `tests/test_server.py`: the add flow uses a fake identify; cover "switch named in the
   message", "unknown device keeps the generic message", "identify not called for ok / busy /
   auth / not_found", "identify raising still gives 422 not_kasa". Existing tests must not
   touch the network: make sure every `DashboardApplication` built in tests gets a fake identify.
7. README: one short paragraph in the Setup section (a non-camera Kasa/TP-Link device is named
   in the error; this sends two small local UDP questions to that one device, ports 9999 and
   20002, nothing leaves the network), and adjust the `not_kasa` wording in the API notes.

## Constraints
- Match existing style. No new dependencies. No logging of device answers.
- `identify_device` runs while the application lock is held (like the camera probe); keep the
  total wait at 1.5 s.
- A dashboard is running from the main checkout on port 1984. Do not stop it, start another on
  that port, or send it write requests. Do not send discovery packets to the real network from
  tests or while working; use fakes.
- This repository is public: placeholder data only (documentation ranges such as `192.0.2.x`,
  MACs like `AA:BB:CC:DD:EE:01`).
- Do not commit; leave changes in the working tree.

## Done when
- [ ] `/usr/bin/python3 -m unittest discover -s tests -t .` passes, including the acceptance file.
- [ ] `git diff --stat HEAD -- tests/test_identify_acceptance.py tests/test_security.py tests/test_weather_acceptance.py tests/test_timezone_acceptance.py www app/store.py app/go2rtc.py` is empty.
- [ ] Final message lists files changed and what you could not verify.

## Context Codex needs
- Current code in `DashboardApplication.add`:
  `if result not in ("ok", "busy"): raise StoreError("not_kasa", "The address did not answer as a Kasa camera.", 422)`.
- Observed on a real network: plugs/switches answer port 20002 with
  `{"result": {"device_model": "HS200(US)", "device_type": "SMART.KASASWITCH", ...}}` after a
  16-byte header; older ones also answer port 9999 with `system.get_sysinfo` containing
  `model` and `mic_type`; Kasa cameras answer 9999 with
  `{"system": {"get_sysinfo": {"system": {"model": "KC410S(US)", "type": "IOT.IPCAMERA", ...}, "err_code": 0}}}`.
- `DashboardApplication.add` — step 5 says `Application` nowhere else; the class is in `app/server.py`.
