"""Bounded local Kasa control using legacy TCP or KLAP, without dependencies."""

import hashlib
import hmac
import http.client
from http.cookies import SimpleCookie
import io
import ipaddress
import json
import math
import os
import socket
import time

from app.aes import cbc_decrypt, cbc_encrypt

_MAX_RESPONSE = 8192
_ERROR_MESSAGES = {
    "unreachable": "The device could not be reached.",
    "auth": "The device did not accept the credentials.",
    "protocol": "The device returned an invalid response.",
    "unsupported": "This device does not support single-device control.",
}


class ControlError(Exception):
    """Only fixed, public-safe messages may cross the control boundary."""

    def __init__(self, code):
        self.code = code if isinstance(code, str) and code in _ERROR_MESSAGES else "protocol"
        super().__init__(_ERROR_MESSAGES[self.code])


def auth_hash(user, password):
    return hashlib.sha256(hashlib.sha1(user.encode("utf-8")).digest() +
                          hashlib.sha1(password.encode("utf-8")).digest()).digest()


def server_hash(local, remote, auth):
    return hashlib.sha256(local + remote + auth).digest()


def client_hash(local, remote, auth):
    return hashlib.sha256(remote + local + auth).digest()


class KlapSession:
    def __init__(self, local, remote, auth):
        if any(not isinstance(value, bytes) or len(value) != size
               for value, size in ((local, 16), (remote, 16), (auth, 32))):
            raise ControlError("protocol")
        material = local + remote + auth
        self.key = hashlib.sha256(b"lsk" + material).digest()[:16]
        full_iv = hashlib.sha256(b"iv" + material).digest()
        self.iv_prefix = full_iv[:12]
        self.sequence = int.from_bytes(full_iv[12:], "big", signed=True)
        self.signature_key = hashlib.sha256(b"ldk" + material).digest()[:28]

    def encrypt(self, data):
        try:
            # Wrap the signed 32-bit counter without overflowing its wire representation.
            self.sequence = (self.sequence + 1 + 2**31) % 2**32 - 2**31
            sequence = self.sequence.to_bytes(4, "big", signed=True)
            cipher = cbc_encrypt(self.key, self.iv_prefix + sequence, data)
            signature = hashlib.sha256(self.signature_key + sequence + cipher).digest()
            return self.sequence, signature + cipher
        except Exception:
            raise ControlError("protocol") from None

    def decrypt(self, seq, body):
        try:
            if (type(seq) is not int or not isinstance(body, bytes) or
                    len(body) > _MAX_RESPONSE or len(body) < 48 or (len(body) - 32) % 16):
                raise ValueError()
            sequence = seq.to_bytes(4, "big", signed=True)
            # KLAP response prefixes are discarded, as required by the device protocol.
            return cbc_decrypt(self.key, self.iv_prefix + sequence, body[32:])
        except Exception:
            raise ControlError("protocol") from None


def _invalid_constant(value):
    raise ValueError()


def _json_bytes(obj):
    try:
        return json.dumps(obj, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except Exception:
        raise ControlError("protocol") from None


def _json_reply(data):
    try:
        if not isinstance(data, bytes) or len(data) > _MAX_RESPONSE:
            raise ValueError()
        return json.loads(data.decode("utf-8"), parse_constant=_invalid_constant)
    except Exception:
        raise ControlError("protocol") from None


def legacy_frame(obj):
    plain = _json_bytes(obj)
    if len(plain) + 4 > _MAX_RESPONSE:
        raise ControlError("protocol")
    key, cipher = 171, bytearray()
    for value in plain:
        key ^= value
        cipher.append(key)
    return len(cipher).to_bytes(4, "big") + bytes(cipher)


def legacy_unframe(data):
    if (not isinstance(data, bytes) or not 4 < len(data) <= _MAX_RESPONSE or
            int.from_bytes(data[:4], "big") != len(data) - 4):
        raise ControlError("protocol")
    key, plain = 171, bytearray()
    for value in data[4:]:
        plain.append(key ^ value)
        key = value
    return _json_reply(bytes(plain))


def _validate_on(on):
    if on is not None and type(on) is not bool:
        raise ControlError("protocol")


def legacy_request(on):
    _validate_on(on)
    if on is None:
        return {"system": {"get_sysinfo": {}}}
    return {"system": {"set_relay_state": {"state": int(on)}}}


def smart_request(on):
    _validate_on(on)
    if on is None:
        return {"method": "get_device_info", "params": None}
    return {"method": "set_device_info", "params": {"device_on": on}}


def _check_error(reply, field):
    if not isinstance(reply, dict) or type(reply.get(field)) is not int or reply[field] != 0:
        raise ControlError("protocol")


def parse_state(mode, reply):
    try:
        if mode == "legacy":
            details = reply["system"]["get_sysinfo"]
            _check_error(details, "err_code")
            if "children" in details and "relay_state" not in details:
                raise ControlError("unsupported")
            state = details["relay_state"]
            if type(state) is not int or state not in (0, 1):
                raise ControlError("protocol")
            return bool(state)
        if mode == "smart":
            _check_error(reply, "error_code")
            state = reply["result"]["device_on"]
            if type(state) is not bool:
                raise ControlError("protocol")
            return state
    except ControlError:
        raise
    except Exception:
        raise ControlError("protocol") from None
    raise ControlError("protocol")


def _check_set_reply(mode, reply):
    try:
        if mode == "legacy":
            _check_error(reply["system"]["set_relay_state"], "err_code")
        else:
            _check_error(reply, "error_code")
    except ControlError:
        raise
    except Exception:
        raise ControlError("protocol") from None


class _Deadline:
    def __init__(self, end):
        self.end = end

    def remaining(self):
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            raise ControlError("unreachable")
        return remaining


class _SocketReader(io.RawIOBase):
    """Reapply the absolute deadline for every HTTP header/body socket read."""

    def __init__(self, connection):
        super().__init__()
        self.connection = connection

    def readable(self):
        return True

    def readinto(self, buffer):
        return self.connection.recv_into(buffer)

    def close(self):
        if not self.closed:
            super().close()
            self.connection.release_reader()


class _DeadlineSocket:
    def __init__(self, connection, deadline):
        self.connection = connection
        self.deadline = deadline
        self.readers = 0
        self.closed = False

    def recv_into(self, buffer):
        self.connection.settimeout(self.deadline.remaining())
        return self.connection.recv_into(buffer)

    def sendall(self, data):
        self.connection.settimeout(self.deadline.remaining())
        return self.connection.sendall(data)

    def makefile(self, mode):
        self.readers += 1
        return io.BufferedReader(_SocketReader(self))

    def close(self):
        self.closed = True
        if self.readers == 0:
            self.connection.close()

    def release_reader(self):
        # HTTPConnection may close before a Connection: close response is consumed.
        self.readers -= 1
        if self.closed and self.readers == 0:
            self.connection.close()


def _close(connection):
    if connection is not None:
        try:
            connection.close()
        except Exception:
            pass


def _validate_ip(ip):
    try:
        if not isinstance(ip, str):
            raise ValueError()
        address = ipaddress.IPv4Address(ip)
        if address.is_multicast or address.is_unspecified or int(address) == 0xffffffff:
            raise ValueError()
    except Exception:
        raise ControlError("unreachable") from None


class DeviceClient:
    """The injected factories use HTTPConnection/create_connection signatures.

    HTTP fakes provide connect, request, getresponse and close, plus a sock attribute
    (None is sufficient). TCP fakes provide settimeout, sendall, recv and close.
    Successful read paths are remembered per client and IP; sessions are not retained.
    """

    def __init__(self, user, password, timeout=2.0, opener=None, connector=None, random=os.urandom):
        if (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0):
            raise ControlError("unreachable")
        if not isinstance(user, str) or not isinstance(password, str):
            raise ControlError("auth")
        try:
            self._auths = tuple(dict.fromkeys(auth_hash(name, secret) for name, secret in (
                (user, password), (user.lower(), password),
                ("kasa@tp-link.net", "kasaSetup"), ("", ""))))
        except Exception:
            raise ControlError("auth") from None
        self.timeout = timeout
        self._opener = opener if opener is not None else http.client.HTTPConnection
        self._connector = connector if connector is not None else socket.create_connection
        self._random = random
        self._paths = {}

    def _deadline(self, budget):
        return _Deadline(min(budget.end, time.monotonic() + self.timeout))

    def _receive(self, connection, size, deadline):
        data = bytearray()
        while len(data) < size:
            connection.settimeout(deadline.remaining())
            chunk = connection.recv(size - len(data))
            deadline.remaining()
            if not isinstance(chunk, bytes) or not chunk or len(chunk) > size - len(data):
                raise ControlError("protocol")
            data.extend(chunk)
        return bytes(data)

    def _legacy(self, ip, on, budget):
        connection = None
        deadline = self._deadline(budget)
        try:
            connection = self._connector((ip, 9999), timeout=deadline.remaining())
            connection.settimeout(deadline.remaining())
            connection.sendall(legacy_frame(legacy_request(on)))
            header = self._receive(connection, 4, deadline)
            size = int.from_bytes(header, "big")
            if not 0 < size <= _MAX_RESPONSE - 4:
                raise ControlError("protocol")
            reply = legacy_unframe(header + self._receive(connection, size, deadline))
            if on is None:
                return parse_state("legacy", reply)
            _check_set_reply("legacy", reply)
        finally:
            _close(connection)

    def _post(self, connection, path, data, cookie, deadline):
        response = None
        try:
            if connection.sock is None:
                connection.timeout = deadline.remaining()
                connection.connect()
            if connection.sock is not None and not isinstance(connection.sock, _DeadlineSocket):
                connection.sock = _DeadlineSocket(connection.sock, deadline)
            deadline.remaining()
            headers = {"Content-Type": "application/octet-stream"}
            if cookie is not None:
                headers["Cookie"] = cookie
            connection.request("POST", path, body=data, headers=headers)
            deadline.remaining()
            response = connection.getresponse()
            deadline.remaining()
            if response.status != 200:
                raise ControlError("protocol")
            response_headers = response.getheaders()
            lengths = [value for name, value in response_headers if name.lower() == "content-length"]
            if lengths and (len(lengths) != 1 or not lengths[0].isascii() or
                            not lengths[0].isdigit() or int(lengths[0]) > _MAX_RESPONSE):
                raise ControlError("protocol")
            body = response.read(_MAX_RESPONSE + 1)
            deadline.remaining()
            if (not isinstance(body, bytes) or len(body) > _MAX_RESPONSE or
                    (lengths and len(body) != int(lengths[0]))):
                raise ControlError("protocol")
            return body, response_headers
        finally:
            _close(response)

    def _cookie(self, headers):
        for name, value in headers:
            if name.lower() == "set-cookie":
                cookies = SimpleCookie()
                cookies.load(value)
                if "TP_SESSIONID" in cookies:
                    value = cookies["TP_SESSIONID"].coded_value
                    if (value and len(value) <= 1024 and
                            all(0x21 <= ord(char) <= 0x7e and char != ";" for char in value)):
                        return "TP_SESSIONID=" + value
        raise ControlError("protocol")

    def _klap_request(self, connection, session, cookie, mode, on, deadline):
        request = smart_request(on) if mode == "smart" else legacy_request(on)
        seq, data = session.encrypt(_json_bytes(request))
        body, _ = self._post(connection, "/app/request?seq=" + str(seq), data, cookie, deadline)
        return _json_reply(session.decrypt(seq, body))

    def _klap(self, ip, on, mode, budget):
        connection = None
        deadline = self._deadline(budget)
        try:
            connection = self._opener(ip, 80, timeout=deadline.remaining())
            local = self._random(16)
            if not isinstance(local, bytes) or len(local) != 16:
                raise ControlError("protocol")
            body, headers = self._post(connection, "/app/handshake1", local, None, deadline)
            if len(body) != 48:
                raise ControlError("protocol")
            remote, received_hash = body[:16], body[16:]
            auth = next((value for value in self._auths
                         if hmac.compare_digest(received_hash, server_hash(local, remote, value))), None)
            if auth is None:
                raise ControlError("auth")
            cookie = self._cookie(headers)
            self._post(connection, "/app/handshake2", client_hash(local, remote, auth), cookie, deadline)
            session = KlapSession(local, remote, auth)
            mode = mode or "smart"
            reply = self._klap_request(connection, session, cookie, mode, on, deadline)
            if on is None:
                # A smart error is terminal; a different JSON dialect can be probed safely.
                if mode == "smart" and (not isinstance(reply, dict) or "error_code" not in reply or
                        (type(reply.get("error_code")) is int and reply["error_code"] == 0 and
                         (not isinstance(reply.get("result"), dict) or
                          "device_on" not in reply["result"]))):
                    mode = "legacy"
                    reply = self._klap_request(connection, session, cookie, mode, None, deadline)
                return parse_state(mode, reply), mode
            _check_set_reply(mode, reply)
            reply = self._klap_request(connection, session, cookie, mode, None, deadline)
            return parse_state(mode, reply), mode
        finally:
            _close(connection)

    def _read(self, ip, budget):
        remembered = self._paths.get(ip)
        paths = [remembered] if remembered else []
        for path in (("legacy", "legacy"), ("klap", None)):
            if not remembered or path[0] != remembered[0]:
                paths.append(path)
        for index, (transport, mode) in enumerate(paths):
            try:
                if transport == "legacy":
                    state, mode = self._legacy(ip, None, budget), "legacy"
                else:
                    state, mode = self._klap(ip, None, mode, budget)
                budget.remaining()
                self._paths[ip] = (transport, mode)
                return state
            except OSError:
                error = ControlError("unreachable")
            except ControlError as caught:
                error = caught
            except Exception:
                error = ControlError("protocol")
            self._paths.pop(ip, None)
            if error.code not in ("unreachable", "protocol") or index == len(paths) - 1:
                raise error from None
        raise ControlError("unreachable")

    def read(self, ip):
        _validate_ip(ip)
        return self._read(ip, _Deadline(time.monotonic() + 2 * self.timeout))

    def set(self, ip, on):
        _validate_ip(ip)
        if type(on) is not bool:
            raise ControlError("protocol")
        budget = _Deadline(time.monotonic() + 2 * self.timeout)
        try:
            if ip not in self._paths:
                self._read(ip, budget)
            transport, mode = self._paths[ip]
            if transport == "legacy":
                self._legacy(ip, on, budget)
                state = self._legacy(ip, None, budget)
            else:
                state, _ = self._klap(ip, on, mode, budget)
            budget.remaining()
            return state
        except OSError:
            self._paths.pop(ip, None)
            raise ControlError("unreachable") from None
        except ControlError:
            self._paths.pop(ip, None)
            raise
        except Exception:
            self._paths.pop(ip, None)
            raise ControlError("protocol") from None
