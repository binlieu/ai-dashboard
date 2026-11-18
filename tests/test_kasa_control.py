"""Fake devices only: no test opens a real socket or HTTP connection."""

import hashlib
import http.client
import json
import socket
import unittest
from unittest.mock import Mock, patch

from app.aes import cbc_encrypt
from app.kasa_control import (ControlError, DeviceClient, KlapSession, auth_hash, legacy_frame,
                              legacy_request, legacy_unframe, parse_state, server_hash,
                              client_hash, smart_request)

IP = "192.0.2.10"
OTHER_IP = "192.0.2.11"
USER, PASSWORD = "User@example.test", "placeholder-password"
LOCAL, REMOTE = bytes(range(16)), bytes(range(16, 32))
COOKIE = "TP_SESSIONID=placeholder-session"


def encoded(reply):
    return json.dumps(reply, separators=(",", ":")).encode()


class FakeResponse:
    def __init__(self, body=b"", status=200, headers=()):
        self.body, self.status, self.headers = body, status, list(headers)
        self.closed = False
        self.sizes = []

    def read(self, size):
        self.sizes.append(size)
        return self.body[:size]

    def getheaders(self):
        return self.headers

    def close(self):
        self.closed = True


class FakeTcp:
    def __init__(self, device):
        self.device = device
        self.data = b""
        self.closed = False
        self.timeouts, self.sizes = [], []

    def settimeout(self, value):
        self.timeouts.append(value)

    def sendall(self, frame):
        request = legacy_unframe(frame)
        self.device.events.append(("legacy", request))
        self.data = legacy_frame(self.device.reply("legacy", request))
        if self.device.tcp_transform:
            self.data = self.device.tcp_transform(self.data)

    def recv(self, size):
        self.sizes.append(size)
        size = min(size, self.device.fragment_size)
        result, self.data = self.data[:size], self.data[size:]
        return result

    def close(self):
        self.closed = True


class FakeHttp:
    def __init__(self, device):
        self.device = device
        self.sock = None
        self.closed = False
        self.response = None
        self.local = self.session = None

    def connect(self):
        if self.device.http_error:
            raise self.device.http_error

    def request(self, method, path, body, headers):
        if method != "POST":
            raise AssertionError("Only POST is expected")
        self.device.http_requests.append((path, body, dict(headers)))
        cookie_headers = []
        if path == "/app/handshake1":
            self.local = body
            self.session = KlapSession(body, REMOTE, self.device.auth)
            reply = REMOTE + server_hash(body, REMOTE, self.device.auth)
            cookie_headers = [("Set-Cookie", COOKIE + "; Path=/; HttpOnly; SameSite=Strict")]
        else:
            if headers.get("Cookie") != COOKIE:
                raise AssertionError("Missing cookie or cookie attributes were sent")
            if path == "/app/handshake2":
                if body != client_hash(self.local, REMOTE, self.device.auth):
                    raise AssertionError("Wrong handshake2 hash")
                reply = b""
            elif path.startswith("/app/request?seq="):
                sequence = int(path.split("=", 1)[1])
                sequence_bytes = sequence.to_bytes(4, "big", signed=True)
                signature = hashlib.sha256(self.session.signature_key + sequence_bytes + body[32:]).digest()
                if signature != body[:32]:
                    raise AssertionError("Wrong request signature")
                request = json.loads(self.session.decrypt(sequence, body))
                self.device.events.append(("klap", request))
                reply = self.device.reply(self.device.mode, request)
                # Response prefixes are deliberately unrelated to the request signature.
                reply = bytes(32) + cbc_encrypt(self.session.key,
                    self.session.iv_prefix + sequence_bytes, encoded(reply))
            else:
                raise AssertionError("Unexpected HTTP path")
        response = FakeResponse(reply, headers=cookie_headers)
        if self.device.http_transform:
            response = self.device.http_transform(path, response)
        self.response = response
        self.device.responses.append(response)

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True
        if self.response is not None:
            self.response.close()


class FakeDevice:
    def __init__(self, legacy=True, mode="smart", credentials=(USER, PASSWORD), state=False):
        self.legacy, self.mode, self.state = legacy, mode, state
        self.auth = auth_hash(*credentials)
        self.apply_writes = True
        self.events, self.http_requests = [], []
        self.sockets, self.connections, self.responses = [], [], []
        self.tcp_attempts, self.http_attempts = [], []
        self.tcp_error = self.http_error = None
        self.tcp_transform = self.http_transform = None
        self.fragment_size = 8192

    def connector(self, address, timeout):
        self.tcp_attempts.append((address, timeout))
        if self.tcp_error:
            raise self.tcp_error
        if not self.legacy:
            raise ConnectionRefusedError("PRIVATE")
        connection = FakeTcp(self)
        self.sockets.append(connection)
        return connection

    def opener(self, ip, port, timeout):
        self.http_attempts.append(((ip, port), timeout))
        connection = FakeHttp(self)
        self.connections.append(connection)
        return connection

    def client(self, **kwargs):
        return DeviceClient(USER, PASSWORD, opener=self.opener, connector=self.connector,
                            random=lambda size: LOCAL, **kwargs)

    def reply(self, mode, request):
        if mode == "legacy":
            if "system" not in request:
                return {"system": {"err_code": -1}}
            if "get_sysinfo" in request["system"]:
                return {"system": {"get_sysinfo": {"relay_state": int(self.state), "err_code": 0}}}
            on = request["system"]["set_relay_state"]["state"]
            if self.apply_writes:
                self.state = bool(on)
            return {"system": {"set_relay_state": {"err_code": 0}}}
        if request["method"] == "get_device_info":
            return {"error_code": 0, "result": {"device_on": self.state}}
        if self.apply_writes:
            self.state = request["params"]["device_on"]
        return {"error_code": 0}


class ClientTests(unittest.TestCase):
    def assert_closed(self, device):
        self.assertTrue(all(connection.closed for connection in device.sockets + device.connections))
        self.assertTrue(all(response.closed for response in device.responses))

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(ControlError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn("PRIVATE", str(caught.exception))

    def test_legacy_read_and_set_use_fragmented_frames_and_read_back(self):
        device = FakeDevice()
        device.fragment_size = 1
        client = device.client()
        self.assertIs(client.read(IP), False)
        self.assertIs(client.set(IP, True), True)
        self.assertIs(client.set(IP, False), False)
        self.assertEqual([request for _, request in device.events],
            [legacy_request(None), legacy_request(True), legacy_request(None),
             legacy_request(False), legacy_request(None)])
        self.assertEqual(device.http_attempts, [])
        self.assertTrue(all(address == (IP, 9999) for address, _ in device.tcp_attempts))
        self.assert_closed(device)

    def test_set_without_a_path_reads_before_sending_any_write(self):
        for legacy, mode in ((True, "legacy"), (False, "smart"), (False, "legacy")):
            with self.subTest(legacy=legacy, mode=mode):
                device = FakeDevice(legacy=legacy, mode=mode)
                client = device.client()
                self.assertIs(client.set(IP, True), True)
                request = legacy_request if mode == "legacy" else smart_request
                requests = [value for _, value in device.events]
                expected = [request(None), request(True), request(None)]
                if not legacy and mode == "legacy":
                    expected.insert(0, smart_request(None))
                self.assertEqual(requests, expected)
                self.assert_closed(device)

    def test_set_returns_reported_state_instead_of_requested_state(self):
        for legacy, mode in ((True, "legacy"), (False, "smart"), (False, "legacy")):
            device = FakeDevice(legacy=legacy, mode=mode)
            device.apply_writes = False
            self.assertIs(device.client().set(IP, True), False)
            self.assert_closed(device)

    def test_refused_reset_timeout_and_protocol_legacy_failures_try_klap(self):
        for failure in (ConnectionRefusedError("PRIVATE"), ConnectionResetError("PRIVATE"),
                        socket.timeout("PRIVATE"), None):
            device = FakeDevice(state=True)
            device.tcp_error = failure
            if failure is None:
                device.tcp_transform = lambda frame: b"\x00\x00\x00\x03bad"
            self.assertIs(device.client().read(IP), True)
            self.assertEqual(len(device.http_attempts), 1)
            self.assert_closed(device)

    def test_klap_smart_cookie_sequence_and_remembered_transport(self):
        device = FakeDevice(legacy=False, state=True)
        client = device.client()
        self.assertIs(client.read(IP), True)
        self.assertIs(client.read(IP), True)
        self.assertEqual(len(device.tcp_attempts), 1)
        self.assertEqual(len(device.http_attempts), 2)
        self.assertTrue(all(address == (IP, 80) for address, _ in device.http_attempts))
        self.assertEqual([path for path, _, _ in device.http_requests].count("/app/handshake1"), 2)
        self.assertTrue(all(headers.get("Cookie") == COOKIE for path, _, headers in device.http_requests
                            if path != "/app/handshake1"))
        self.assert_closed(device)

    def test_klap_legacy_json_probe_and_remembered_dialect(self):
        device = FakeDevice(legacy=False, mode="legacy")
        client = device.client()
        self.assertIs(client.read(IP), False)
        self.assertIs(client.read(IP), False)
        self.assertIs(client.set(IP, True), True)
        self.assertEqual([value for _, value in device.events],
            [smart_request(None), legacy_request(None), legacy_request(None),
             legacy_request(True), legacy_request(None)])
        self.assertEqual(len(device.tcp_attempts), 1)
        self.assert_closed(device)

    def test_paths_are_per_ip_and_per_client(self):
        device = FakeDevice(legacy=False)
        client = device.client()
        client.read(IP)
        client.read(OTHER_IP)
        device.client().read(IP)
        self.assertEqual([address[0] for address, _ in device.tcp_attempts], [IP, OTHER_IP, IP])
        self.assert_closed(device)

    def test_remembered_transport_failure_can_relearn_a_read_path(self):
        device = FakeDevice(legacy=False)
        client = device.client()
        client.read(IP)
        device.legacy = True
        device.http_error = ConnectionRefusedError("PRIVATE")
        self.assertIs(client.read(IP), False)
        attempts = len(device.http_attempts)
        self.assertIs(client.read(IP), False)
        self.assertEqual(len(device.http_attempts), attempts)
        self.assert_closed(device)

    def test_credentials_lower_case_default_and_blank(self):
        for credentials in ((USER, PASSWORD), (USER.lower(), PASSWORD),
                            ("kasa@tp-link.net", "kasaSetup"), ("", "")):
            with self.subTest(credentials=credentials):
                device = FakeDevice(legacy=False, credentials=credentials)
                self.assertIs(device.client().read(IP), False)
                self.assertEqual(len(device.connections), 1)
                self.assert_closed(device)

    def test_wrong_credentials_are_auth_and_do_not_send_commands(self):
        device = FakeDevice(legacy=False, credentials=("other@example.test", "wrong-placeholder"))
        self.assert_code("auth", device.client().set, IP, True)
        self.assertEqual([path for path, _, _ in device.http_requests], ["/app/handshake1"])
        self.assertEqual(device.events, [])
        self.assert_closed(device)

    def test_set_does_not_replay_a_failed_write_on_another_path(self):
        device = FakeDevice()
        client = device.client()
        client.read(IP)
        device.tcp_error = ConnectionResetError("PRIVATE")
        self.assert_code("unreachable", client.set, IP, True)
        self.assertEqual(device.http_attempts, [])
        device.legacy, device.tcp_error = False, None
        self.assertIs(client.set(IP, True), True)
        self.assertEqual([value for transport, value in device.events if transport == "klap"],
                         [smart_request(None), smart_request(True), smart_request(None)])
        self.assert_closed(device)

    def test_invalid_ip_timeout_and_state_never_open_connections(self):
        device = FakeDevice()
        client = device.client()
        for ip in (None, 123, b"192.0.2.10", [], {}, "example.test", "::1", "192.0.2.010",
                   "192.0.2", "192.0.2.10 ", "0.0.0.0", "224.0.0.1", "255.255.255.255"):
            self.assert_code("unreachable", client.read, ip)
            self.assert_code("unreachable", client.set, ip, True)
        for timeout in (None, True, 0, -1, "2", float("nan"), float("inf")):
            self.assert_code("unreachable", device.client, timeout=timeout)
        for on in (None, 0, 1, "true", [], {}):
            self.assert_code("protocol", client.set, IP, on)
        self.assertEqual(device.tcp_attempts + device.http_attempts, [])

    def test_both_transports_unreachable_are_a_sanitized_error(self):
        device = FakeDevice(legacy=False)
        device.http_error = OSError("PRIVATE 192.0.2.10 credentials")
        self.assert_code("unreachable", device.client().read, IP)
        self.assert_closed(device)

    def test_truncated_oversized_and_garbage_legacy_frames_fall_back(self):
        for bad in (b"", b"\x00\x00", b"\x00\x00\x00\x20short",
                    b"\x00\x00\x20\x00", b"\x00\x00\x00\x03bad",
                    legacy_frame(None), legacy_frame({"system": {"get_sysinfo": {"relay_state": True,
                                                                               "err_code": 0}}})):
            device = FakeDevice()
            device.tcp_transform = lambda frame: bad
            self.assertIs(device.client().read(IP), False)
            self.assertEqual(len(device.connections), 1)
            self.assert_closed(device)

    def test_unsupported_multi_outlet_is_terminal_and_never_written(self):
        device = FakeDevice()
        device.tcp_transform = lambda frame: legacy_frame(
            {"system": {"get_sysinfo": {"children": [], "err_code": 0}}})
        self.assert_code("unsupported", device.client().set, IP, True)
        self.assertEqual(device.http_attempts, [])
        self.assertEqual([value for _, value in device.events], [legacy_request(None)])
        self.assert_closed(device)

    def test_malformed_handshakes_and_cookies_never_reach_a_command(self):
        for body, headers in ((b"", []), (bytes(47), []), (bytes(49), []), (bytes(8193), []),
                              (None, []), (None, [("Set-Cookie", "OTHER=x")]),
                              (None, [("Set-Cookie", "TP_SESSIONID=")]),
                              (None, [("Set-Cookie", "TP_SESSIONID=x\r\nInjected: PRIVATE")])):
            device = FakeDevice(legacy=False)

            def transform(path, response):
                if path == "/app/handshake1":
                    if body is not None:
                        response.body = body
                    response.headers = headers
                return response

            device.http_transform = transform
            self.assert_code("protocol", device.client().read, IP)
            self.assertEqual(len(device.http_requests), 1)
            self.assert_closed(device)

    def test_bad_encrypted_and_json_klap_replies_are_sanitized(self):
        for bad in (b"", bytes(40), bytes(49), bytes(8193), bytes(48)):
            device = FakeDevice(legacy=False)

            def transform(path, response):
                if path.startswith("/app/request"):
                    response.body = bad
                return response

            device.http_transform = transform
            self.assert_code("protocol", device.client().read, IP)
            self.assert_closed(device)

    def test_smart_error_is_not_retried_as_a_legacy_command(self):
        device = FakeDevice(legacy=False)
        device.reply = lambda mode, request: {"error_code": -1501, "msg": "PRIVATE"}
        self.assert_code("protocol", device.client().read, IP)
        self.assertEqual(device.events, [("klap", smart_request(None))])
        self.assert_closed(device)

    def test_set_error_or_bad_readback_is_not_reported_as_success(self):
        for legacy in (True, False):
            for bad_read in (False, True):
                device = FakeDevice(legacy=legacy)
                client = device.client()
                client.read(IP)
                normal = device.reply

                def reply(mode, request):
                    is_read = request == (legacy_request(None) if mode == "legacy" else smart_request(None))
                    if is_read != bad_read:
                        return normal(mode, request)
                    if mode == "legacy":
                        command = "get_sysinfo" if is_read else "set_relay_state"
                        return {"system": {command: {"err_code": -1, "msg": "PRIVATE"}}}
                    return {"error_code": -1, "msg": "PRIVATE"}

                device.reply = reply
                self.assert_code("protocol", client.set, IP, True)
                self.assert_closed(device)

    def test_non_200_responses_are_not_followed(self):
        for status in (301, 302, 307, 401, 404, 500):
            for stage in ("/app/handshake1", "/app/handshake2", "/app/request"):
                device = FakeDevice(legacy=False)

                def transform(path, response):
                    if path.startswith(stage):
                        response.status = status
                        response.headers.append(("Location", "http://example.test/PRIVATE"))
                    return response

                device.http_transform = transform
                self.assert_code("protocol", device.client().read, IP)
                self.assertEqual(len(device.connections), 1)
                self.assert_closed(device)

    def test_http_content_length_is_bounded_and_must_match(self):
        for length in ("8193", "-1", "PRIVATE", "49", "47", "４８"):
            device = FakeDevice(legacy=False)

            def transform(path, response):
                response.headers.append(("Content-Length", length))
                return response

            device.http_transform = transform
            self.assert_code("protocol", device.client().read, IP)
            self.assert_closed(device)

    def test_random_and_parser_failures_do_not_escape_or_leak(self):
        for random in (lambda size: b"short", lambda size: None, Mock(side_effect=ValueError("PRIVATE"))):
            device = FakeDevice(legacy=False)
            client = DeviceClient(USER, PASSWORD, opener=device.opener, connector=device.connector, random=random)
            self.assert_code("protocol", client.read, IP)
            self.assert_closed(device)
        for method in ("request", "getresponse"):
            device = FakeDevice(legacy=False)
            original = device.opener

            def opener(*args, **kwargs):
                connection = original(*args, **kwargs)
                setattr(connection, method, Mock(side_effect=ValueError("PRIVATE")))
                return connection

            device.opener = opener
            self.assert_code("protocol", device.client().read, IP)
            self.assert_closed(device)


class HelperTests(unittest.TestCase):
    def test_fixed_control_error_messages_for_arbitrary_input(self):
        for code in ("unreachable", "auth", "protocol", "unsupported"):
            error = ControlError(code)
            self.assertEqual(error.code, code)
            self.assertEqual(str(error), str(ControlError(code)))
            self.assertEqual(error.args, (str(error),))
        for value in ("PRIVATE", None, [], {}):
            self.assertEqual(str(ControlError(value)), str(ControlError("protocol")))

    def test_unicode_auth_uses_utf8_and_raw_sha1_digests(self):
        user, password = "tést@example.test", "pāssword"
        expected = hashlib.sha256(hashlib.sha1(user.encode()).digest() +
                                  hashlib.sha1(password.encode()).digest()).digest()
        self.assertEqual(auth_hash(user, password), expected)

    def test_signed_sequence_wrap_and_decrypt_input_validation(self):
        session = KlapSession(LOCAL, REMOTE, auth_hash(USER, PASSWORD))
        session.sequence = 2**31 - 1
        sequence, body = session.encrypt(b"placeholder")
        self.assertEqual(sequence, -2**31)
        self.assertEqual(session.decrypt(sequence, body), b"placeholder")
        for seq, data in ((True, body), (2**31, body), (-2**31 - 1, body), (0, None),
                          (0, bytearray(body)), (0, bytes(8193))):
            with self.assertRaises(ControlError) as caught:
                session.decrypt(seq, data)
            self.assertEqual(caught.exception.code, "protocol")
        for seeds in ((LOCAL[:15], REMOTE, bytes(32)), (LOCAL, None, bytes(32)),
                      (LOCAL, REMOTE, bytes(31))):
            with self.assertRaises(ControlError):
                KlapSession(*seeds)

    def test_request_and_state_types_are_strict(self):
        for on in (0, 1, "true", [], {}):
            for function in (legacy_request, smart_request):
                with self.assertRaises(ControlError):
                    function(on)
        for reply in ({"error_code": False, "result": {"device_on": True}},
                      {"error_code": 0.0, "result": {"device_on": True}},
                      {"error_code": "0", "result": {"device_on": True}},
                      {"error_code": 0, "result": {"device_on": None}}):
            with self.assertRaises(ControlError):
                parse_state("smart", reply)
        for code in (False, 0.0, "0", None):
            with self.assertRaises(ControlError):
                parse_state("legacy", {"system": {"get_sysinfo": {"err_code": code, "relay_state": 1}}})
        self.assertIs(parse_state("legacy", {"system": {"get_sysinfo":
            {"err_code": 0, "children": [], "relay_state": 1}}}), True)

    def test_legacy_size_boundary_and_nonstandard_json(self):
        boundary = legacy_frame("x" * 8186)
        self.assertEqual(len(boundary), 8192)
        self.assertEqual(legacy_unframe(boundary), "x" * 8186)
        for data in (boundary + b"x", bytearray(boundary), None):
            with self.assertRaises(ControlError):
                legacy_unframe(data)
        for obj in ("x" * 8187, float("nan"), object()):
            with self.assertRaises(ControlError):
                legacy_frame(obj)
        for plain in (b"NaN", b"Infinity", b"\xff", b"[" * 2000 + b"]" * 2000):
            key, cipher = 171, bytearray()
            for byte in plain:
                key ^= byte
                cipher.append(key)
            with self.assertRaises(ControlError):
                legacy_unframe(len(cipher).to_bytes(4, "big") + cipher)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def advance(self, delay, timeout):
        self.now += min(delay, timeout)
        if delay >= timeout:
            raise socket.timeout("PRIVATE")


class WireSocket:
    """A byte stream for the real HTTPConnection/HTTPResponse parser, never a socket."""

    def __init__(self, device, handler, clock=None, delay=0, fragment_size=8192,
                 connection_close=False, transform=None):
        self.device, self.handler = device, handler
        self.clock, self.delay, self.fragment_size = clock, delay, fragment_size
        self.connection_close, self.transform = connection_close, transform
        self.closed = False
        self.close_count = 0
        self.timeout = None
        self.timeouts = []
        self.data, self.pending = b"", b""
        self.path, self.headers = None, None

    def settimeout(self, value):
        self.timeout = value
        self.timeouts.append(value)

    def setsockopt(self, *args):
        pass

    def sendall(self, data):
        if self.closed:
            raise OSError("PRIVATE socket closed")
        if self.path is None:
            header, data = data.split(b"\r\n\r\n", 1)
            lines = header.decode("ascii").split("\r\n")
            self.path = lines[0].split()[1]
            self.headers = dict(line.split(": ", 1) for line in lines[1:])
        self.pending += data
        if len(self.pending) == int(self.headers["Content-Length"]):
            self.handler.request("POST", self.path, self.pending, self.headers)
            response = self.handler.getresponse()
            headers = list(response.headers) + [("Content-Length", str(len(response.body)))]
            if self.connection_close:
                headers.append(("Connection", "close"))
            wire = ("HTTP/1.1 %d OK\r\n" % response.status).encode()
            wire += b"".join((name + ": " + value + "\r\n").encode() for name, value in headers)
            wire += b"\r\n" + response.body
            if self.transform:
                wire = self.transform(self.path, wire)
            self.data = wire
            response.close()
            self.path, self.headers, self.pending = None, None, b""

    def recv_into(self, buffer):
        if self.closed:
            raise OSError("PRIVATE socket closed before response consumed")
        if self.clock:
            self.clock.advance(self.delay, self.timeout)
        size = min(len(buffer), len(self.data), self.fragment_size)
        buffer[:size], self.data = self.data[:size], self.data[size:]
        return size

    def close(self):
        self.closed = True
        self.close_count += 1


class WireAndDeadlineTests(unittest.TestCase):
    def run_wire_read(self, device, **wire_options):
        streams = []
        handler = FakeHttp(device)

        def dial(address, timeout, source_address=None):
            if address == (IP, 9999):
                raise ConnectionRefusedError("PRIVATE")
            self.assertEqual(address, (IP, 80))
            stream = WireSocket(device, handler, **wire_options)
            streams.append(stream)
            return stream

        with patch("app.kasa_control.socket.create_connection", side_effect=dial):
            client = DeviceClient(USER, PASSWORD, random=lambda size: LOCAL)
            result = client.read(IP)
        self.assertTrue(all(stream.closed for stream in streams))
        self.assertTrue(all(stream.close_count == 1 for stream in streams))
        return result, streams

    def test_default_http_transport_handles_keepalive_and_connection_close(self):
        for connection_close in (False, True):
            device = FakeDevice(legacy=False, state=True)
            result, streams = self.run_wire_read(device, connection_close=connection_close)
            self.assertIs(result, True)
            self.assertEqual(len(streams), 3 if connection_close else 1)

    def test_real_http_parser_rejects_short_bodies_oversized_headers_and_bad_status_lines(self):
        transforms = (lambda path, wire: wire[:-1],
                      lambda path, wire: b"PRIVATE\r\n\r\n",
                      lambda path, wire: b"HTTP/1.1 200 OK\r\nX-Private: " + b"x" * 65537 + b"\r\n\r\n")
        for transform in transforms:
            device = FakeDevice(legacy=False)
            handler = FakeHttp(device)
            streams = []

            def dial(address, timeout, source_address=None):
                if address[1] == 9999:
                    raise ConnectionRefusedError()
                stream = WireSocket(device, handler, transform=transform)
                streams.append(stream)
                return stream

            with patch("app.kasa_control.socket.create_connection", side_effect=dial):
                with self.assertRaises(ControlError) as caught:
                    DeviceClient(USER, PASSWORD, random=lambda size: LOCAL).read(IP)
            self.assertEqual(caught.exception.code, "protocol")
            self.assertTrue(all(stream.closed for stream in streams))

    def test_slow_http_headers_cannot_extend_the_total_deadline(self):
        device = FakeDevice(legacy=False)
        handler, clock, streams = FakeHttp(device), FakeClock(), []

        def dial(address, timeout, source_address=None):
            if address[1] == 9999:
                clock.advance(timeout, timeout)
            stream = WireSocket(device, handler, clock=clock, delay=0.3, fragment_size=1)
            streams.append(stream)
            return stream

        with patch("app.kasa_control.socket.create_connection", side_effect=dial), \
                patch("app.kasa_control.time.monotonic", side_effect=lambda: clock.now):
            with self.assertRaises(ControlError) as caught:
                DeviceClient(USER, PASSWORD, timeout=1, random=lambda size: LOCAL).read(IP)
        self.assertEqual(caught.exception.code, "unreachable")
        self.assertAlmostEqual(clock.now, 2)
        self.assertEqual(len(streams), 1)
        self.assertTrue(streams[0].closed)
        self.assertEqual(streams[0].timeouts, sorted(streams[0].timeouts, reverse=True))

    def test_slow_http_body_cannot_reset_the_deadline_between_packets(self):
        device = FakeDevice(legacy=False)
        handler, clock, streams = FakeHttp(device), FakeClock(), []

        class SlowBody(WireSocket):
            def recv_into(self, buffer):
                # Let the parser consume headers promptly, then drip the body.
                self.fragment_size = self.data.find(b"\r\n\r\n") + 4 if self.delay == 0 else 1
                result = super().recv_into(buffer)
                self.delay = 0.3
                return result

        def dial(address, timeout, source_address=None):
            if address[1] == 9999:
                raise ConnectionRefusedError()
            stream = SlowBody(device, handler, clock=clock)
            streams.append(stream)
            return stream

        with patch("app.kasa_control.socket.create_connection", side_effect=dial), \
                patch("app.kasa_control.time.monotonic", side_effect=lambda: clock.now):
            with self.assertRaises(ControlError) as caught:
                DeviceClient(USER, PASSWORD, timeout=1, random=lambda size: LOCAL).read(IP)
        self.assertEqual(caught.exception.code, "unreachable")
        self.assertAlmostEqual(clock.now, 1)
        self.assertTrue(all(stream.closed for stream in streams))

    def test_set_shares_one_budget_with_the_prerequisite_read_and_readback(self):
        device, clock = FakeDevice(), FakeClock()
        attempts = []

        def dial(address, timeout):
            attempts.append(timeout)
            clock.advance(0.75, timeout)
            return device.connector(address, timeout)

        client = DeviceClient(USER, PASSWORD, timeout=1, opener=device.opener, connector=dial)
        with patch("app.kasa_control.time.monotonic", side_effect=lambda: clock.now):
            with self.assertRaises(ControlError) as caught:
                client.set(IP, True)
        self.assertEqual(caught.exception.code, "unreachable")
        self.assertAlmostEqual(clock.now, 2)
        self.assertEqual(attempts, [1, 1, 0.5])
        self.assertEqual(device.events, [("legacy", legacy_request(None)), ("legacy", legacy_request(True))])
        self.assertTrue(all(connection.closed for connection in device.sockets))

    def test_tcp_deadline_is_reapplied_to_each_fragment(self):
        device, clock = FakeDevice(), FakeClock()
        device.http_error = ConnectionRefusedError()
        device.fragment_size = 1

        class SlowTcp(FakeTcp):
            def recv(self, size):
                clock.advance(0.3, self.timeouts[-1])
                return super().recv(size)

        connection = SlowTcp(device)
        client = DeviceClient(USER, PASSWORD, timeout=1, opener=device.opener,
                              connector=lambda address, timeout: connection)
        with patch("app.kasa_control.time.monotonic", side_effect=lambda: clock.now):
            with self.assertRaises(ControlError) as caught:
                client.read(IP)
        self.assertEqual(caught.exception.code, "unreachable")
        self.assertAlmostEqual(clock.now, 1)
        self.assertTrue(connection.closed)
        self.assertEqual(connection.timeouts, sorted(connection.timeouts, reverse=True))


if __name__ == "__main__":
    unittest.main()
