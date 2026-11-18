"""Real handlers against a fake Unix HTTP upstream, without cameras or binaries.

Use ephemeral TCP/Unix listeners normally. When the OS sandbox denies bind(),
use connected socket pairs for the same HTTP and upgrade assertions instead.
No test is skipped, and the acceptance request-gate tests remain untouched.
"""

import base64
import copy
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler
import json
import io
import os
from pathlib import Path
import socket
import socketserver
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from app.go2rtc import UnixHTTPClient, UnixHTTPConnection
from app.server import DashboardApplication, DashboardHandler, DashboardServer
from app.store import CameraStore
from app.weather import WeatherError, WeatherService
from tests.test_weather import PLACE, SETTINGS, forecast_payload

MAC1, MAC2 = 'aa:bb:cc:dd:ee:01', 'aa:bb:cc:dd:ee:02'
WS_KEY = 'dGhlIHNhbXBsZSBub25jZQ=='


class UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.owner.requests.append((self.command, self.path, dict(self.headers)))
        if self.path.startswith('/api/ws?'):
            accept = base64.b64encode(hashlib.sha1((self.headers['Sec-WebSocket-Key'] +
                     '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            self.send_response(101)
            self.send_header('Upgrade', 'websocket')
            self.send_header('Connection', 'Upgrade')
            self.send_header('Sec-WebSocket-Accept', accept)
            self.send_header('Access-Control-Allow-Origin', '*')  # Must never reach the browser.
            self.end_headers()
            self.wfile.write(b'UPSTREAM_BUFFERED')
            self.close_connection = True
            try:
                while True:
                    data = self.rfile.read1(65536)
                    if not data:
                        return
                    self.wfile.write(data)
            except OSError:
                return
        body = b'{}' if self.path == '/api/streams' else b'\xff\xd8fake-jpeg\xff\xd9'
        self.send_response(200)
        self.send_header('Content-Type', 'application/json' if self.path == '/api/streams' else 'image/jpeg')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def do_PUT(self):
        self.server.owner.requests.append((self.command, self.path, dict(self.headers)))
        self.send_error(500)

    do_PATCH = do_DELETE = do_POST = do_PUT


class UnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def handle_error(self, *args):
        pass


class FakeUpstream:
    def __init__(self, path):
        self.path, self.requests = str(path), []
        self.thread, self.pairs, self.workers = None, [], []
        self.server = UnixServer(self.path, UpstreamHandler, bind_and_activate=False)
        self.server.owner = self
        try:
            self.server.server_bind()
            self.server.server_activate()
        except PermissionError:
            self.server.server_close()
        else:
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()

    def connect(self):
        if self.thread is not None:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(5)
            connection.connect(self.path)
            return connection
        client, upstream = socket.socketpair()
        self.pairs.extend((client, upstream))
        worker = threading.Thread(target=UpstreamHandler, args=(upstream, ('local', 0), self.server), daemon=True)
        self.workers.append(worker)
        worker.start()
        return client

    def connection(self, path, timeout=15):
        owner = self
        class Connection(UnixHTTPConnection):
            def connect(self):
                self.sock = owner.connect()
                self.sock.settimeout(self.timeout)
        return Connection(path, timeout)

    def close(self):
        if self.thread is not None:
            self.server.shutdown()
        self.server.server_close()
        for connection in self.pairs:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        for worker in self.workers:
            worker.join(timeout=1)
        if os.path.exists(self.path):
            os.unlink(self.path)


class FakeBridge:
    def __init__(self, upstream):
        self.upstream = upstream
        self.socket_path = upstream.path
        self.client = UnixHTTPClient(upstream.path, connection_factory=upstream.connection)
        self.applied, self.failures = [], 0

    def apply(self, cameras):
        self.applied.append(copy.deepcopy(cameras))
        if self.failures:
            self.failures -= 1
            raise RuntimeError('synthetic readiness failure')
        if not self.client.ready():
            raise RuntimeError('fake bridge unavailable')

    def frame(self, camera_id):
        return self.client.frame(camera_id)

    def open_socket(self):
        return self.upstream.connect()


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='kcd-tests-', dir='/tmp')
        self.root = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)
        self.www = self.root/'www'
        self.www.mkdir()
        (self.www/'index.html').write_text('<h1>Camera</h1>')
        (self.root/'private.js').write_text('private data')
        (self.www/'escape.js').symlink_to(self.root/'private.js')
        self.upstream = FakeUpstream(self.root/'fake.sock')
        self.addCleanup(self.upstream.close)
        self.bridge = FakeBridge(self.upstream)
        self.store = CameraStore(self.root/'data/cameras.json', {'CAMERA_MAC': MAC1})
        self.lookup, self.sweep, self.probe = Mock(return_value='192.0.2.12'), Mock(), Mock(return_value='ok')
        self.identify = Mock(return_value=None)
        self.application = DashboardApplication(self.store, self.bridge, 'dummy@example.com', 'ZHVtbXk=',
            lookup=self.lookup, sweep=self.sweep, probe=self.probe, identify=self.identify,
            initial_cameras=[{'id': 'cam1', 'ip': '192.0.2.11'}])
        self.server = DashboardServer(('127.0.0.1', 0), self.application, self.www, bind_and_activate=False)
        self.thread, self.front_sockets, self.front_workers = None, [], []
        try:
            self.server.server_bind()
            self.server.server_activate()
        except PermissionError:
            self.server.socket.close()
            self.server.server_port = 1984
        else:
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
        self.addCleanup(self.close_front)
        self.host = '127.0.0.1:%s' % self.server.server_port

    def close_front(self):
        if self.thread is not None:
            self.server.shutdown()
        self.server.server_close()
        for connection in self.front_sockets:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        for worker in self.front_workers:
            worker.join(timeout=1)

    def connect(self):
        if self.thread is not None:
            return socket.create_connection(('127.0.0.1', self.server.server_port), timeout=5)
        client, handler = socket.socketpair()
        client.settimeout(5)
        self.front_sockets.extend((client, handler))
        worker = threading.Thread(target=DashboardHandler,
            args=(handler, ('127.0.0.1', 12345), self.server), daemon=True)
        self.front_workers.append(worker)
        worker.start()
        return client

    def connection(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        connection.sock = self.connect()
        return connection

    def request(self, method, path, body=None, headers=None):
        supplied = {'Host': self.host}
        if method in ('POST', 'PATCH', 'PUT', 'DELETE'):
            supplied['Origin'] = 'http://' + self.host
        if body is not None:
            supplied['Content-Type'] = 'application/json'
            body = json.dumps(body).encode()
        supplied.update(headers or {})
        connection = self.connection()
        try:
            connection.request(method, path, body=body, headers=supplied)
            response = connection.getresponse()
            result = response.status, dict(response.getheaders()), response.read()
            self.assert_headers(result[1])
            return result
        finally:
            connection.close()

    def assert_headers(self, headers):
        lower = {key.lower(): value for key, value in headers.items()}
        self.assertFalse(any(key.startswith('access-control-') for key in lower))
        for key, value in {'x-content-type-options': 'nosniff', 'referrer-policy': 'no-referrer',
                           'cache-control': 'no-store', 'cross-origin-resource-policy': 'same-origin'}.items():
            self.assertEqual(lower[key], value)
        self.assertIn("frame-ancestors 'none'", lower['content-security-policy'])

    def test_static_head_and_keepalive(self):
        status, headers, body = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertEqual(body, b'<h1>Camera</h1>')
        status, headers, body = self.request('HEAD', '/')
        self.assertEqual((status, body), (200, b''))
        self.assertEqual(headers['Content-Length'], '15')
        connection = self.connection()
        try:
            for target in ('/', '/api/cameras'):
                connection.request('GET', target, headers={'Host': self.host})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertIsNotNone(response.getheader('Content-Length'))
                response.read()
        finally:
            connection.close()

    def test_paths_and_symlinks_cannot_escape(self):
        for target in ('/../private.js', '/%2e%2e/private.js', '//api/cameras', '/.env'):
            with self.subTest(target=target):
                self.assertIn(self.request('GET', target)[0], (403, 404))
        self.assertEqual(self.request('GET', '/escape.js')[0], 404)
        self.assertEqual(self.request('GET', '/not-a-file.txt')[0], 404)

    def test_camera_metadata(self):
        status, _, body = self.request('GET', '/api/cameras')
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data['layout'], 'auto')
        self.assertEqual(data['max'], 16)
        self.assertEqual(data['cameras'], [{'id': 'cam1', 'name': 'Camera 1', 'mac': MAC1,
                                           'ip': '192.0.2.11', 'available': True}])
        self.application.ips.clear()
        camera = json.loads(self.request('GET', '/api/cameras')[2])['cameras'][0]
        self.assertIsNone(camera['ip'])
        self.assertFalse(camera['available'])

    def test_post_probe_before_apply_and_busy_is_success(self):
        order = []
        self.probe.side_effect = lambda *args: order.append('probe') or 'busy'
        original = self.bridge.apply
        def apply(cameras):
            order.append('apply')
            original(cameras)
        self.bridge.apply = apply
        status, _, body = self.request('POST', '/api/cameras', {'mac': MAC2.upper(), 'name': ' Back '})
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body)['name'], 'Back')
        self.assertEqual(json.loads(body)['mac'], MAC2)
        self.assertEqual(order, ['probe', 'apply'])
        self.sweep.assert_not_called()
        self.identify.assert_not_called()

    def test_post_identifies_non_camera_without_changing_state(self):
        self.probe.return_value = 'unreachable'
        before = self.store.snapshot(), self.application.ips.copy()
        for identity, message in (({'kind': 'switch', 'model': 'HS200'},
                                   'This is a Kasa smart switch (HS200), not a camera.'),
                                  (None, 'The address did not answer as a Kasa camera.')):
            with self.subTest(identity=identity):
                self.identify.reset_mock()
                self.identify.return_value = identity
                status, _, body = self.request('POST', '/api/cameras', {'mac': MAC2})
                self.assertEqual(status, 422)
                self.assertEqual(json.loads(body), {'error': 'not_kasa', 'message': message})
                self.identify.assert_called_once_with('192.0.2.12')
                self.assertEqual((self.store.snapshot(), self.application.ips), before)
                self.assertEqual(self.bridge.applied, [])

    def test_post_identify_failure_keeps_generic_not_kasa_error(self):
        self.probe.return_value = 'unreachable'
        self.identify.side_effect = RuntimeError('synthetic discovery failure')
        status, _, body = self.request('POST', '/api/cameras', {'mac': MAC2})
        self.assertEqual(status, 422)
        self.assertEqual(json.loads(body), {'error': 'not_kasa',
            'message': 'The address did not answer as a Kasa camera.'})
        self.identify.assert_called_once_with('192.0.2.12')
        self.assertEqual(self.bridge.applied, [])

    def test_post_camera_and_auth_paths_do_not_identify(self):
        for result, expected in (('ok', 201), ('busy', 201), ('auth', 422)):
            with self.subTest(result=result):
                self.probe.return_value = result
                status, _, body = self.request('POST', '/api/cameras', {'mac': MAC2})
                self.assertEqual(status, expected)
                self.identify.assert_not_called()
                if status == 201:
                    self.application.remove(json.loads(body)['id'])
        self.lookup.return_value = None
        self.assertEqual(self.request('POST', '/api/cameras', {'mac': MAC2})[0], 404)
        self.identify.assert_not_called()

    def test_post_errors(self):
        cases = [('invalid_mac', 400, {'mac': 'invalid'}), ('duplicate', 409, {'mac': MAC1})]
        for code, expected, body in cases:
            with self.subTest(code=code):
                status, _, response = self.request('POST', '/api/cameras', body)
                self.assertEqual((status, json.loads(response)['error']), (expected, code))
        self.lookup.return_value = None
        status, _, body = self.request('POST', '/api/cameras', {'mac': MAC2})
        self.assertEqual((status, json.loads(body)['error']), (404, 'not_found'))
        self.sweep.assert_called_once()
        self.lookup.return_value = '192.0.2.12'
        for result, code in (('auth', 'auth'), ('unreachable', 'not_kasa')):
            self.probe.return_value = result
            status, _, body = self.request('POST', '/api/cameras', {'mac': MAC2})
            self.assertEqual((status, json.loads(body)['error']), (422, code))
        for index in range(15):
            self.store.add('00:11:22:33:44:%02x' % index)
        status, _, body = self.request('POST', '/api/cameras', {'mac': MAC2})
        self.assertEqual((status, json.loads(body)['error']), (409, 'limit'))
        self.assertEqual(self.bridge.applied, [])

    def test_post_sweep_once_then_success(self):
        self.lookup.side_effect = [None, '192.0.2.12']
        self.assertEqual(self.request('POST', '/api/cameras', {'mac': MAC2})[0], 201)
        self.sweep.assert_called_once()
        self.assertEqual(self.lookup.call_count, 2)

    def test_apply_failure_rolls_back_without_id_reuse(self):
        self.bridge.failures = 1
        status, _, body = self.request('POST', '/api/cameras', {'mac': MAC2})
        self.assertEqual((status, json.loads(body)['error']), (502, 'go2rtc'))
        self.assertEqual(len(self.store.snapshot()['cameras']), 1)
        self.assertEqual([camera['id'] for camera in self.bridge.applied[-1]], ['cam1'])
        self.assertEqual(len(self.bridge.applied), 2)
        status, _, body = self.request('POST', '/api/cameras', {'mac': MAC2})
        self.assertEqual((status, json.loads(body)['id']), (201, 'cam3'))

    def test_rename_delete_and_layout(self):
        status, _, body = self.request('PATCH', '/api/cameras/cam1', {'name': 'New'})
        self.assertEqual((status, json.loads(body)['name']), (200, 'New'))
        self.assertEqual(self.bridge.applied, [])
        self.assertEqual(self.request('PATCH', '/api/cameras/cam9', {'name': 'New'})[0], 404)
        self.assertEqual(self.request('PATCH', '/api/cameras/cam1', {'name': '\n'})[0], 400)
        status, _, body = self.request('PUT', '/api/layout', {'layout': '2x2'})
        self.assertEqual((status, json.loads(body)), (200, {'layout': '2x2'}))
        self.assertEqual(self.request('PUT', '/api/layout', {'layout': 'bad'})[0], 400)
        status, headers, body = self.request('DELETE', '/api/cameras/cam1')
        self.assertEqual((status, headers['Content-Length'], body), (204, '0', b''))
        self.assertEqual(self.request('DELETE', '/api/cameras/cam1')[0], 404)

    def test_weather_routes_cached_response_and_no_bridge_changes(self):
        fetch = Mock(side_effect=[PLACE, forecast_payload()])
        self.application.weather = WeatherService(self.store, fetch, lambda: 100000)
        status, _, body = self.request('GET', '/api/weather')
        self.assertEqual((status, json.loads(body)['status']), (200, 'off'))
        fetch.assert_not_called()
        status, _, body = self.request('PUT', '/api/weather', SETTINGS)
        self.assertEqual((status, json.loads(body)['status']), (200, 'loading'))
        self.application.weather.refresh_once()
        status, _, body = self.request('GET', '/api/weather')
        result = json.loads(body)
        self.assertEqual((status, result['status'], result['forecast']['updated']), (200, 'ok', 100000))
        self.assertNotIn('lat', result)
        self.assertNotIn('lon', result)
        self.assertEqual(self.bridge.applied, [])
        self.assertEqual(self.upstream.requests, [])

    def test_weather_errors_body_limits_and_invalid_json(self):
        service = self.application.weather = WeatherService(self.store, Mock(), lambda: 100000)
        for code, expected in (('place_not_found', 404), ('weather_unavailable', 502)):
            service.fetch.side_effect = WeatherError(code, 'SENTINEL')
            status, _, body = self.request('PUT', '/api/weather', SETTINGS)
            self.assertEqual((status, json.loads(body)['error']), (expected, code))
            self.assertNotIn(b'SENTINEL', body)
        for body, code in ((dict(SETTINGS, enabled=1), 'invalid_settings'),
                           (dict(SETTINGS, place=''), 'invalid_place'),
                           (dict(SETTINGS, units='k'), 'invalid_units')):
            status, _, response = self.request('PUT', '/api/weather', body)
            self.assertEqual((status, json.loads(response)['error']), (400, code))
        self.assertEqual(self.request('PUT', '/api/weather', dict(SETTINGS, place='x'*5000))[0], 413)
        self.assertEqual(self.request('PUT', '/api/weather', [])[0], 400)
        connection = self.connection()
        try:
            connection.request('PUT', '/api/weather', body=b'{', headers={
                'Host': self.host, 'Origin': 'http://' + self.host, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            self.assertEqual(json.loads(response.read())['error'], 'invalid_json')
        finally:
            connection.close()

    def test_weather_geocode_does_not_block_camera_mutation_or_frame(self):
        entered, release = threading.Event(), threading.Event()
        results = []
        def fetch(url):
            entered.set()
            if not release.wait(3):
                raise WeatherError('weather_unavailable')
            return PLACE
        self.application.weather = WeatherService(self.store, fetch, lambda: 100000)
        worker = threading.Thread(target=lambda: results.append(self.request('PUT', '/api/weather', SETTINGS)), daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.request('PATCH', '/api/cameras/cam1', {'name': 'Camera'})[0], 200)
            self.assertEqual(self.request('GET', '/api/frame.jpeg?src=cam1')[0], 200)
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0][0], 200)
        self.assertEqual(self.bridge.applied, [])

    def test_delete_failure_restores_camera(self):
        self.bridge.failures = 1
        self.assertEqual(self.request('DELETE', '/api/cameras/cam1')[0], 502)
        self.assertEqual(len(self.store.snapshot()['cameras']), 1)

    def test_json_body_limits_and_types(self):
        for body, expected in (([], 400), ('text', 400), ({'mac': MAC2, 'name': 'x'*5000}, 413),
                               ({'mac': MAC2, 'name': '\x00'}, 400)):
            self.assertEqual(self.request('POST', '/api/cameras', body)[0], expected)
        connection = self.connection()
        try:
            connection.request('POST', '/api/cameras', body=b'{', headers={
                'Host': self.host, 'Origin': 'http://'+self.host, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            self.assertEqual(json.loads(response.read())['error'], 'invalid_json')
        finally:
            connection.close()

    def test_foreign_origin_and_closed_upstream_surface(self):
        before = len(self.upstream.requests)
        status, _, _ = self.request('POST', '/api/cameras', {'mac': MAC2},
                                   {'Origin': 'http://foreign.example', 'Content-Type': 'text/plain'})
        self.assertEqual(status, 403)
        for target in ('/api/config', '/api/exit', '/api/streams'):
            self.assertIn(self.request('POST', target, {})[0], (403, 404, 405))
        self.assertEqual(len(self.upstream.requests), before)
        self.probe.assert_not_called()
        self.lookup.assert_not_called()

    def test_frames_and_read_only_upstream_client(self):
        status, headers, body = self.request('GET', '/api/frame.jpeg?src=cam1')
        self.assertEqual((status, headers['Content-Type'], body), (200, 'image/jpeg', b'\xff\xd8fake-jpeg\xff\xd9'))
        before = len(self.upstream.requests)
        self.assertEqual(self.request('GET', '/api/frame.jpeg?src=cam9')[0], 404)
        self.assertEqual(len(self.upstream.requests), before)
        self.assertTrue(self.bridge.client.ready())
        self.assertTrue(all(method == 'GET' for method, _, _ in self.upstream.requests))

    def upgrade(self, connection, early=b''):
        request = ('GET /api/ws?src=cam1 HTTP/1.1\r\nHost: %s\r\nOrigin: http://%s\r\n'
                   'Upgrade: websocket\r\nConnection: keep-alive, Upgrade\r\nSec-WebSocket-Key: %s\r\n'
                   'Sec-WebSocket-Version: 13\r\nCookie: private=must-not-forward\r\n'
                   'Authorization: must-not-forward\r\nUser-Agent: must-not-forward\r\n\r\n') % (self.host, self.host, WS_KEY)
        connection.sendall(request.encode()+early)
        data = b''
        while b'\r\n\r\n' not in data:
            data += connection.recv(4096)
        return data.split(b'\r\n\r\n', 1)

    def test_websocket_echo_and_both_buffered_directions(self):
        connection = self.connect()
        try:
            early = b'\x81\x06early\x00'
            headers, data = self.upgrade(connection, early)
            self.assertTrue(headers.startswith(b'HTTP/1.1 101'))
            self.assertNotIn(b'Access-Control-', headers)
            self.assertIn(b'Content-Security-Policy:', headers)
            expected = b'UPSTREAM_BUFFERED'+early
            while len(data) < len(expected):
                data += connection.recv(4096)
            self.assertEqual(data, expected)
            binary = b'\x82\x05\x00\xffnew'
            connection.sendall(binary)
            echoed = b''
            while len(echoed) < len(binary):
                echoed += connection.recv(4096)
            self.assertEqual(echoed, binary)
            forwarded = {key.lower(): value for key, value in self.upstream.requests[-1][2].items()}
            self.assertEqual(forwarded['host'], 'localhost')
            self.assertEqual(forwarded['origin'], 'http://localhost')
            self.assertNotIn('cookie', forwarded)
            self.assertNotIn('authorization', forwarded)
            self.assertNotIn('user-agent', forwarded)
        finally:
            connection.close()

    def test_websocket_limit_and_restarting(self):
        self.server.relay_slots = threading.BoundedSemaphore(1)
        self.assertTrue(self.server.relay_slots.acquire(False))
        connection = self.connect()
        try:
            headers, _ = self.upgrade(connection)
            self.assertTrue(headers.startswith(b'HTTP/1.1 503'))
        finally:
            connection.close()
            self.server.relay_slots.release()
        self.bridge.open_socket = Mock(side_effect=ConnectionRefusedError)
        connection = self.connect()
        try:
            headers, _ = self.upgrade(connection)
            self.assertTrue(headers.startswith(b'HTTP/1.1 503'))
        finally:
            connection.close()

    def test_background_refresh_applies_once_and_never_sweeps(self):
        self.store.add(MAC2)
        self.lookup.side_effect = ['192.0.2.11', '192.0.2.12']
        self.application.refresh_once()
        self.assertEqual(len(self.bridge.applied), 1)
        self.lookup.side_effect = ['192.0.2.11', '192.0.2.12']
        self.application.refresh_once()
        self.assertEqual(len(self.bridge.applied), 1)
        self.lookup.side_effect = ['192.0.2.21', '192.0.2.22']
        self.application.refresh_once()
        self.assertEqual(len(self.bridge.applied), 2)
        self.sweep.assert_not_called()

    def test_ambiguous_framing_and_duplicate_hosts_are_rejected(self):
        cases = [
            ('GET / HTTP/1.1\r\nHost: %s\r\nHost: %s\r\n\r\n' % (self.host, self.host), 403),
            ('POST /api/cameras HTTP/1.1\r\nHost: %s\r\nOrigin: http://%s\r\nContent-Type: application/json\r\n'
             'Content-Length: 2\r\nContent-Length: 3\r\n\r\n{}' % (self.host, self.host), 400),
            ('POST /api/cameras HTTP/1.1\r\nHost: %s\r\nOrigin: http://%s\r\nContent-Type: application/json\r\n'
             'Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n' % (self.host, self.host), 400),
            ('POST /api/cameras HTTP/1.1\r\nHost: %s\r\nOrigin: http://%s\r\nContent-Type: application/json\r\n'
             'Expect: 100-continue\r\nContent-Length: 2\r\n\r\n' % (self.host, self.host), 417),
            ('TRACE / HTTP/1.1\r\nHost: %s\r\n\r\n' % self.host, 405),
        ]
        for request, expected in cases:
            with self.subTest(expected=expected):
                connection = self.connect()
                try:
                    connection.sendall(request.encode())
                    response = http.client.HTTPResponse(connection)
                    response.begin()
                    self.assertEqual(response.status, expected)
                    self.assert_headers(dict(response.getheaders()))
                    self.assertIsNotNone(response.getheader('Content-Length'))
                    response.read()
                finally:
                    connection.close()
        self.assertEqual(self.upstream.requests, [])


class MainTests(unittest.TestCase):
    def setUp(self):
        application = patch('app.__main__.DashboardApplication', side_effect=lambda *args, **kwargs:
            DashboardApplication(*args, identify=Mock(return_value=None), **kwargs))
        application.start()
        self.addCleanup(application.stop)
        self.directory = tempfile.TemporaryDirectory(prefix='kcd-main-', dir='/tmp')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.previous = Path.cwd()
        self.addCleanup(os.chdir, self.previous)
        (self.root/'.env').write_text('KASA_EMAIL=dummy@example.com\nKASA_PASSWORD=synthetic-password\nCAMERA_MACS=\n')

    def test_zero_camera_startup_open_and_clean_shutdown(self):
        from app.__main__ import main
        event, output = Mock(), io.StringIO()
        event.is_set.return_value = False
        with patch('app.__main__.Go2RTC') as bridge, patch('app.__main__.DashboardServer') as server, \
                patch('app.__main__.WeatherService') as weather, \
                patch('app.__main__.threading.Event', return_value=event), patch('app.__main__.threading.Thread') as thread, \
                patch('app.__main__.signal.signal'), patch('app.__main__.webbrowser.open') as browser, \
                patch('sys.stdout', output):
            self.assertEqual(main(['--open'], self.root), 0)
            bridge.return_value.start.assert_called_once_with([])
            bridge.return_value.stop.assert_called_once()
            weather.return_value.start.assert_called_once()
            weather.return_value.stop.assert_called_once()
            server.return_value.shutdown.assert_called_once()
            server.return_value.server_close.assert_called_once()
            thread.return_value.start.assert_called()
            browser.assert_called_once_with('http://127.0.0.1:1984/')
        self.assertIn('Dashboard: http://127.0.0.1:1984/', output.getvalue())
        self.assertNotIn('dummy@example.com', output.getvalue())
        self.assertNotIn('synthetic-password', output.getvalue())
        self.assertEqual(json.loads((self.root/'data/cameras.json').read_text())['cameras'], [])

    def test_port_already_in_use_does_not_start_bridge(self):
        import errno
        from app.__main__ import main
        with patch('app.__main__.Go2RTC') as bridge, \
                patch('app.__main__.DashboardServer', side_effect=OSError(errno.EADDRINUSE, 'occupied')), \
                patch('app.__main__.signal.signal'), patch('sys.stderr', new_callable=io.StringIO) as error:
            self.assertEqual(main([], self.root), 1)
            bridge.return_value.start.assert_not_called()
            self.assertIn('port is already in use', error.getvalue())

    def test_required_keys_and_bad_port_never_echo_values(self):
        from app.__main__ import main
        for settings, expected in [('KASA_PASSWORD=synthetic-password\n', 'KASA_EMAIL'),
                                   ('KASA_EMAIL=dummy@example.com\n', 'KASA_PASSWORD'),
                                   ('KASA_EMAIL=dummy@example.com\nKASA_PASSWORD=synthetic-password\nDASHBOARD_PORT=99999999999\n', 'DASHBOARD_PORT')]:
            (self.root/'.env').write_text(settings)
            with self.subTest(expected=expected), patch('app.__main__.Go2RTC') as bridge, \
                    patch('app.__main__.signal.signal'), patch('sys.stderr', new_callable=io.StringIO) as error:
                self.assertEqual(main([], self.root), 1)
                bridge.assert_not_called()
                self.assertIn(expected, error.getvalue())
                self.assertNotIn('synthetic-password', error.getvalue())
                self.assertNotIn('dummy@example.com', error.getvalue())
