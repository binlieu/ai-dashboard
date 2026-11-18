# Codex task spec — Device control 1/3: local Kasa plug/switch protocol

Effort: xhigh

## Goal
A standard-library-only client that reads and sets the on/off state of Kasa plugs and switches
on the local network, over both the legacy protocol (TCP 9999) and KLAP (HTTP port 80).

## Scope — touch ONLY these
- `app/aes.py` (new), `app/kasa_control.py` (new)
- `tests/test_aes.py`, `tests/test_kasa_control.py` (new, your own cases)

Nothing else. No server, store, page or README changes in this task.

## Acceptance tests (written by Claude — do NOT modify)
- `tests/test_control_acceptance.py` — `/usr/bin/python3 -m unittest tests.test_control_acceptance`.
  Do not modify any `tests/*_acceptance.py` or `tests/test_security.py`. If one looks wrong,
  stop and say why instead of changing it. (`tests/test_security_devices_acceptance.py` is for
  task 2 and is expected to fail until then.)

## What to do

### `app/aes.py` — AES-128-CBC with PKCS#7 padding, Python 3.9 stdlib only
- `cbc_encrypt(key, iv, data) -> bytes`, `cbc_decrypt(key, iv, data) -> bytes`.
- 16-byte key and IV, `bytes` in and out. Wrong key/IV length, ciphertext that is empty or not
  a multiple of 16, or invalid padding → `ValueError` (message must not include data).
- Build the S-box at import (computed or literal); verify against FIPS-197 in your own tests.
- This talks to devices on the LAN only; clarity over speed, messages are under 2 KB.

### `app/kasa_control.py`
Pure helpers (fixed by the acceptance tests):
- `ControlError(code)` — codes `unreachable`, `auth`, `protocol`, `unsupported`. `str()` is a
  fixed sentence per code; never include device text, addresses or credentials.
- `auth_hash(user, password)` = `sha256(sha1(user) + sha1(password))` (UTF-8, raw digests).
- `server_hash(local, remote, auth)` = `sha256(local + remote + auth)`;
  `client_hash(local, remote, auth)` = `sha256(remote + local + auth)`.
- `KlapSession(local, remote, auth)`: key = `sha256(b"lsk"+local+remote+auth)[:16]`;
  `full = sha256(b"iv"+local+remote+auth)`, IV prefix = `full[:12]`, starting sequence = the
  last 4 bytes as a signed big-endian int; signature key = `sha256(b"ldk"+local+remote+auth)[:28]`.
  `encrypt(data) -> (seq, body)`: increment the sequence first; IV = prefix + seq (4 bytes,
  signed big-endian); `cipher = cbc_encrypt(...)`; `body = sha256(sigkey + seqbytes + cipher) + cipher`.
  `decrypt(seq, body) -> bytes`: drop the first 32 bytes, decrypt with that seq's IV; anything
  malformed → `ControlError("protocol")`.
- `legacy_frame(obj)`: compact JSON (`separators=(",", ":")`), autokey XOR (key starts 171),
  4-byte big-endian length prefix. `legacy_unframe(data)`: exact inverse; wrong length, over
  8192 bytes, bad JSON → `ControlError("protocol")`.
- `legacy_request(on)` / `smart_request(on)` with `on` in `None` (read), `True`, `False`.
- `parse_state(mode, reply) -> bool` for modes `"legacy"` and `"smart"`; strict types; error
  codes other than 0, missing fields, or a multi-outlet device (`children` present without a
  single `relay_state`) → `ControlError` (`unsupported` for the children case, else `protocol`).

Client:
- `DeviceClient(user, password, timeout=4.0, opener=None, connector=None, random=os.urandom)`.
  `opener`/`connector` are injection points for tests (HTTP exchange and TCP exchange); the
  defaults use `http.client.HTTPConnection(ip, 80)` and `socket.create_connection((ip, 9999))`.
- `read(ip) -> bool` and `set(ip, on) -> bool` (`set` sends the command, then reads the state
  back and returns what the device reports).
- Accept only a unicast IPv4 string (same rule as `app/identify.py`); else `ControlError("unreachable")`.
- Order: try legacy TCP first. Connection refused / reset / timeout / a `protocol` failure on
  legacy → try KLAP. Remember per IP which path worked (in memory) and try it first next time.
- KLAP exchange: `POST /app/handshake1` with a 16-byte random local seed → 200 with exactly 48
  bytes (16-byte remote seed + 32-byte hash) and a `TP_SESSIONID` cookie in `Set-Cookie`.
  The hash must equal `server_hash` for one of these credentials, tried in order: the account;
  the account with the user name lower-cased; (`kasa@tp-link.net`, `kasaSetup`); blank/blank.
  No match → `ControlError("auth")`. Then `POST /app/handshake2` with `client_hash`, sending
  the cookie (name=value only) → 200. Then `POST /app/request?seq=<seq>` with the encrypted
  body and cookie → 200, body decrypted with the same seq.
  Over KLAP send the smart request first; if the decrypted reply has no `error_code` key or is
  not a smart reply, send the legacy request JSON over the same session (older "IOT" firmware
  speaks the legacy JSON inside KLAP). Remember which worked.
  One new handshake per `read`/`set` call is fine; do not keep sessions between calls.
- Bounds: responses capped at 8192 bytes; total time per call at most `2 * timeout`; every
  socket/connection closed; all network and parsing failures become `ControlError`; never log.
- No redirects followed, no proxies (use `http.client` directly), plain HTTP to the LAN IP only.

## Constraints
- Python 3.9 standard library only. No new files beyond scope.
- Tests use fakes only: an in-process fake device built from the helper functions is expected
  (cover: legacy path, KLAP smart path, KLAP legacy-JSON path, wrong credentials → `auth`,
  default-credential fallback, truncated/oversized/garbage replies, refused connection,
  remembered path, `set` returning the read-back state, sockets closed). Never contact a real
  address; a dashboard is running on port 1984 — leave it alone.
- Public repository: placeholder data only. Do not commit.

## Done when
- [ ] `/usr/bin/python3 -m unittest tests.test_control_acceptance tests.test_aes tests.test_kasa_control` passes.
- [ ] `/usr/bin/python3 -m unittest discover -s tests -t .` passes except `tests.test_security_devices_acceptance` (task 2).
- [ ] `git status --short` shows only the four new files.
- [ ] Final message lists what you could not verify.
