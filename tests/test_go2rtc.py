import http.client
import io
import os
from pathlib import Path
import socket
import ssl
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.go2rtc import Go2RTC, Go2RTCError, UnixHTTPClient, choose_socket_path, probe_camera, remove_socket, render_config


class FakeChild:
    def __init__(self, output=b''):
        self.stdout = io.BytesIO(output)
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class Go2RTCTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name).resolve()
        self.cameras = [{'id': 'cam1', 'ip': '192.0.2.11'}, {'id': 'cam2', 'ip': None},
                        {'id': 'cam3', 'ip': '192.0.2.13'}]

    def test_render_literal_placeholders_and_no_network_listeners(self):
        with patch.dict(os.environ, KASA_USER_ENC='SENTINEL_USER', KASA_PASS_B64='SENTINEL_SECRET'):
            config = render_config('/tmp/private/api.sock', self.cameras)
        self.assertIn('${KASA_USER_ENC}', config)
        self.assertIn('${KASA_PASS_B64}', config)
        self.assertNotIn('SENTINEL_', config)
        self.assertIn('  cam1: kasa://', config)
        self.assertIn('  cam3: kasa://', config)
        self.assertNotIn('  cam2:', config)
        self.assertNotIn('static_dir', config)
        self.assertEqual([line.strip() for line in config.splitlines() if 'listen:' in line and 'unix_' not in line],
                         ['listen: ""'] * 3)
        self.assertIn('streams: {}', render_config('/tmp/socket', []))

    def test_render_rejects_stream_injection(self):
        for camera in ({'id': 'cam1\nexec:bad', 'ip': '192.0.2.1'},
                       {'id': 'cam1', 'ip': '192.0.2.1/exec:bad'}):
            with self.assertRaises(ValueError):
                render_config('/tmp/sock', [camera])

    def test_long_socket_path_fallback(self):
        path, directory = choose_socket_path(self.repo / ('x' * 120))
        try:
            self.assertIsNotNone(directory)
            self.assertLessEqual(len(os.fsencode(path)), 100)
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertTrue(directory.name.startswith('kcd-'))
        finally:
            directory.rmdir()

    def test_short_socket_path(self):
        path, directory = choose_socket_path('/tmp/kcd-test')
        self.assertEqual(path, Path('/tmp/kcd-test/run/api.sock').resolve())
        self.assertIsNone(directory)

    def test_non_socket_file_not_removed(self):
        path = self.repo / 'api.sock'
        path.write_text('keep')
        with self.assertRaises(Go2RTCError):
            remove_socket(path)
        self.assertEqual(path.read_text(), 'keep')

    def test_stop_without_start_leaves_a_running_instances_socket(self):
        # A second ./start.sh that fails on the port must not unlink the live socket.
        first = Go2RTC(self.repo, 'user@example.com', 'secret')
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        try:
            listener.bind(str(first.socket_path))
        except PermissionError:
            bound = False
        else:
            bound = True
        second = Go2RTC(self.repo, 'user@example.com', 'secret')
        with patch('app.go2rtc.remove_socket', wraps=remove_socket) as remove:
            second.stop()
            remove.assert_not_called()
        if bound:
            self.assertTrue(stat.S_ISSOCK(os.stat(str(first.socket_path)).st_mode))

    def test_apply_rewrites_config_before_every_spawn(self):
        snapshots, children = [], []
        def popen(args, **kwargs):
            snapshots.append((self.repo / 'run/go2rtc.yaml').read_text())
            self.assertEqual(args, [str(self.repo / 'bin/go2rtc'), '-c', 'run/go2rtc.yaml'])
            self.assertEqual(kwargs['cwd'], str(self.repo))
            self.assertEqual(kwargs['env']['KASA_USER_ENC'], 'qa%2Bcamera%40example.com')
            self.assertEqual(kwargs['env']['KASA_PASS_B64'], 'c2VjcmV0MQ%3D%3D')
            child = FakeChild()
            children.append(child)
            return child
        ready = Mock()
        ready.ready.return_value = True
        bridge = Go2RTC(self.repo, 'qa+camera@example.com', 'secret1', popen=popen, client=ready)
        self.addCleanup(bridge.stop)
        bridge.apply(self.cameras)
        (self.repo / 'run/go2rtc.yaml').write_text('SENTINEL_REWRITE')
        bridge.apply(self.cameras[:1])
        self.assertEqual(len(snapshots), 2)
        self.assertTrue(all('${KASA_PASS_B64}' in text and 'SENTINEL_REWRITE' not in text for text in snapshots))
        self.assertNotIn('cam3:', snapshots[-1])
        self.assertEqual(children[0].poll(), -15)
        self.assertEqual(stat.S_IMODE((self.repo/'run/go2rtc.yaml').stat().st_mode), 0o600)

    def test_crash_restarts_render_again_and_back_off(self):
        snapshots = []
        def failing_spawn(*args, **kwargs):
            snapshots.append((self.repo/'run/go2rtc.yaml').read_text())
            (self.repo/'run/go2rtc.yaml').write_text('STALE_CONFIG')
            raise OSError('synthetic crash')
        bridge = Go2RTC(self.repo, 'qa@example.com', 'secret1', popen=failing_spawn, logger=lambda _: None)
        class Stop:
            def __init__(self): self.delays, self.stopped = [], False
            def is_set(self): return self.stopped
            def set(self): self.stopped = True
            def wait(self, delay):
                if self.stopped or (delay == 0.2 and len(self.delays) == 4): return True
                if delay != 0.2: self.delays.append(delay)
                return False
        stop = Stop()
        bridge._stop = stop
        bridge._cameras = self.cameras
        self.addCleanup(bridge.stop)
        bridge._supervise()
        self.assertEqual(stop.delays, [1, 2, 5, 10])
        self.assertEqual(len(snapshots), 4)
        self.assertTrue(all('${KASA_USER_ENC}' in text and 'STALE_CONFIG' not in text for text in snapshots))

    def test_readiness_failure_terminates_child(self):
        child = FakeChild()
        client = Mock()
        client.ready.return_value = False
        bridge = Go2RTC(self.repo, 'qa@example.com', 'secret1', popen=lambda *a, **k: child,
                        client=client, ready_timeout=0)
        self.addCleanup(bridge.stop)
        with self.assertRaises(Go2RTCError):
            bridge.apply([])
        self.assertEqual(child.poll(), -15)

    def test_output_redacts_url_and_standalone_credentials(self):
        output = []
        bridge = Go2RTC(self.repo, 'qa+camera@example.com', 'secret1', logger=output.append)
        self.addCleanup(bridge.stop)
        bridge._forward_output(FakeChild(b'kasa://qa%2Bcamera%40example.com:c2VjcmV0MQ%3D%3D@192.0.2.11 secret1\n'))
        self.assertEqual(output, ['kasa://***@192.0.2.11 ***'])

    def test_http_client_only_issues_get(self):
        connection = Mock()
        response = connection.getresponse.return_value
        response.status = 200
        response.getheader.return_value = 'image/jpeg'
        response.read.return_value = b'jpeg'
        client = UnixHTTPClient('/tmp/not-real', connection_factory=lambda *a, **k: connection)
        self.assertTrue(client.ready())
        self.assertEqual(client.frame('cam1'), (200, 'image/jpeg', b'jpeg'))
        self.assertEqual([call.args for call in connection.request.call_args_list],
                         [('GET', '/api/streams'), ('GET', '/api/frame.jpeg?src=cam1')])
        with patch('app.go2rtc.MAX_FRAME', 3), self.assertRaises(Go2RTCError):
            client.frame('cam1')

    def test_mp4_stream_decodes_chunked_response_and_closes_the_connection(self):
        from app.go2rtc import UnixHTTPConnection

        client_socket, upstream = socket.socketpair()
        self.addCleanup(upstream.close)
        self.addCleanup(client_socket.close)
        connection = UnixHTTPConnection(self.repo / 'fake.sock')
        connection.sock = client_socket
        client_socket.settimeout(1)
        upstream.settimeout(1)
        client = UnixHTTPClient(self.repo / 'fake.sock', connection_factory=Mock(return_value=connection))
        bridge = Go2RTC(self.repo, 'placeholder@example.com', 'placeholder', client=client)
        self.addCleanup(bridge.stop)
        upstream.sendall(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n'
                         b'3\r\nabc\r\n2\r\nde\r\n0\r\n\r\n')
        stream = bridge.open_mp4('cam4')
        self.addCleanup(stream.close)
        self.assertTrue(upstream.recv(4096).startswith(b'GET /api/stream.mp4?src=cam4 HTTP/1.1\r\n'))
        self.assertEqual(stream.read(4), b'abcd')
        self.assertEqual(stream.read1(4), b'e')
        stream.close()
        self.assertEqual(upstream.recv(1), b'')

    def test_mp4_stream_rejects_invalid_ids_and_unsuccessful_status(self):
        connection = Mock()
        response = connection.getresponse.return_value
        response.status = 503
        client = UnixHTTPClient(self.repo / 'fake.sock', connection_factory=Mock(return_value=connection))
        bridge = Go2RTC(self.repo, 'placeholder@example.com', 'placeholder', client=client)
        self.addCleanup(bridge.stop)
        with self.assertRaises(ValueError):
            bridge.open_mp4('cam1&audio=all')
        client.connection_factory.assert_not_called()
        with self.assertRaises(Go2RTCError):
            bridge.open_mp4('cam1')
        response.close.assert_called_once()
        response.read.assert_not_called()
        connection.close.assert_called_once()

    def test_camera_probe_statuses_headers_only_and_tls_retry(self):
        for status, expected in ((200, 'ok'), (503, 'busy'), (401, 'auth'), (500, 'unreachable')):
            with self.subTest(status=status), patch('app.go2rtc.http.client.HTTPSConnection') as constructor:
                connection = constructor.return_value
                connection.getresponse.return_value.status = status
                self.assertEqual(probe_camera('192.0.2.11', 'qa@example.com', 'c2VjcmV0MQ=='), expected)
                connection.getresponse.return_value.read.assert_not_called()
                self.assertEqual(connection.request.call_args.args[0], 'GET')
                connection.close.assert_called_once()
        with patch('app.go2rtc.http.client.HTTPSConnection') as constructor, \
                patch('app.go2rtc.ssl._create_unverified_context', side_effect=[Mock(), Mock()]) as contexts:
            first, second = Mock(), Mock()
            first.request.side_effect = ssl.SSLError('old device TLS')
            second.getresponse.return_value.status = 200
            constructor.side_effect = [first, second]
            self.assertEqual(probe_camera('192.0.2.11', 'qa@example.com', 'c2VjcmV0MQ=='), 'ok')
            self.assertEqual(contexts.call_count, 2)
        with patch('app.go2rtc.http.client.HTTPSConnection', side_effect=OSError):
            self.assertEqual(probe_camera('192.0.2.11', 'qa@example.com', 'x'), 'unreachable')
            self.assertEqual(probe_camera('invalid', 'qa@example.com', 'x'), 'unreachable')
