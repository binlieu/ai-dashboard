"""Malformed-packet and deadline coverage using only scripted UDP sockets."""

import json
import socket
import unittest
from unittest.mock import Mock, patch

from app.identify import describe, identify_device, parse_identity


IP = '192.0.2.10'


def packet(fields):
    return bytes(16) + json.dumps({'result': fields}).encode('utf-8')


def legacy_packet(details):
    key, result = 171, bytearray()
    for byte in json.dumps({'system': {'get_sysinfo': details}}).encode('utf-8'):
        key ^= byte
        result.append(key)
    return bytes(result)


class ParserTests(unittest.TestCase):
    def test_model_validation_boundaries_and_no_prefix_from_rejected_model(self):
        for model, clean in (('X' * 24, 'X' * 24), ('X' * 25, None),
                             ('aB1 _.+-9(EU)', 'aB1 _.+-9'), ('HS200(US)extra', None),
                             ('HS200(US)\n', None), ('HS200\x00', None),
                             ('HS200<svg>', None), ('HŠ200', None)):
            with self.subTest(model=model):
                self.assertEqual(parse_identity(20002, packet({'device_model': model,
                    'device_type': 'ROUTER'})), {'kind': 'router', 'model': clean})

    def test_remaining_prefixes_and_case_insensitive_type_precedence(self):
        for model, kind in (('lb100', 'light'), ('ec70', 'camera'), ('kd110', 'camera')):
            self.assertEqual(parse_identity(20002, packet({'device_model': model,
                'device_type': 'ROUTER'})), {'kind': kind, 'model': model})
        for device_type, kind in (('plugSwitch camera', 'camera'), ('plugSwitch', 'plug_or_switch'),
                                  ('plug switch', 'switch'), ('light plug', 'plug'),
                                  ('light router', 'light'), ('wifiRouter', 'router'),
                                  ('homeWifiSystem', 'router'), ('hub', 'other')):
            with self.subTest(device_type=device_type):
                self.assertEqual(parse_identity(20002, packet({'device_type': device_type})),
                                 {'kind': kind, 'model': None})

    def test_legacy_type_fallback_and_nested_details_override_outer_fields(self):
        self.assertEqual(parse_identity(9999, legacy_packet({'mic_type': 5, 'type': 'SWITCH'})),
                         {'kind': 'switch', 'model': None})
        details = {'model': 'HS200', 'mic_type': 'SWITCH',
                   'system': {'model': 'KC401', 'type': 'CAMERA'}}
        self.assertEqual(parse_identity(9999, legacy_packet(details)),
                         {'kind': 'camera', 'model': 'KC401'})
        details['system'] = {'err_code': -1}
        self.assertIsNone(parse_identity(9999, legacy_packet(details)))

    def test_bad_shapes_types_encoding_and_deep_json_never_raise(self):
        for value in (None, True, 5, 9999.0, [], {}, object()):
            self.assertIsNone(parse_identity(value, packet({'device_model': 'HS200'})))
            self.assertIsNone(parse_identity(20002, value))
        for payload in (b'null', b'5', b'[]', b'{"result":null}', b'{"result":true}',
                        b'\xff', b'[' * 2000 + b']' * 2000):
            self.assertIsNone(parse_identity(20002, bytes(16) + payload))
        for system in (None, 5, [], {'get_sysinfo': None}):
            key, data = 171, bytearray()
            for byte in json.dumps({'system': system}).encode():
                key ^= byte
                data.append(key)
            self.assertIsNone(parse_identity(9999, data))

    def test_packet_limit_includes_header(self):
        data = packet({'device_model': 'HS200'})
        boundary = data + b' ' * (8192 - len(data))
        self.assertEqual(parse_identity(20002, boundary), {'kind': 'switch', 'model': 'HS200'})
        self.assertIsNone(parse_identity(20002, boundary + b' '))

    def test_camera_without_model(self):
        self.assertEqual(describe({'kind': 'camera', 'model': None}),
            'This looks like a camera, but it did not accept a video connection. '
            'Check that it is online and try again.')


class TimedSocket:
    def __init__(self, replies=()):
        self.now, self.timeout = 0.0, None
        self.replies = list(replies)
        self.sent, self.reads = [], []
        self.closed = False

    def settimeout(self, value):
        self.timeout = value

    def sendto(self, data, address):
        self.sent.append((self.now, data, address))

    def recvfrom(self, size):
        self.reads.append((self.now, self.timeout, size))
        if not self.replies or self.replies[0][0] > self.timeout:
            if self.replies:
                delay, reply = self.replies[0]
                self.replies[0] = (delay - self.timeout, reply)
            self.now += self.timeout
            raise socket.timeout()
        delay, reply = self.replies.pop(0)
        self.now += delay
        return reply

    def close(self):
        self.closed = True


class DeadlineTests(unittest.TestCase):
    def identify(self, connection):
        with patch('app.identify.time.monotonic', side_effect=lambda: connection.now):
            return identify_device(IP, socket_factory=lambda: connection)

    def test_silence_resends_at_half_budget_and_finishes_at_full_budget(self):
        connection = TimedSocket()
        self.assertIsNone(self.identify(connection))
        self.assertEqual([sent[0] for sent in connection.sent], [0, 0, 0.75, 0.75])
        self.assertEqual(connection.reads, [(0, 0.75, 8193), (0.75, 0.75, 8193)])
        self.assertEqual(connection.now, 1.5)
        self.assertTrue(connection.closed)

    def test_noisy_datagrams_do_not_extend_either_deadline(self):
        data = packet({'device_model': 'HS200'})
        connection = TimedSocket([(0.25, (data, ('192.0.2.99', 20002))),
                                  (0.25, (data, (IP, 5353))),
                                  (0.5, (b'bad json', (IP, 20002))),
                                  (0.25, (data, ('192.0.2.99', 9999)))])
        self.assertIsNone(self.identify(connection))
        self.assertEqual([sent[0] for sent in connection.sent], [0, 0, 0.75, 0.75])
        self.assertEqual([read[1] for read in connection.reads], [0.75, 0.5, 0.25, 0.75, 0.5, 0.25])
        self.assertEqual(connection.now, 1.5)
        self.assertTrue(connection.closed)

    def test_first_accepted_answer_wins_without_resend(self):
        connection = TimedSocket([(0.1, (packet({'device_model': 'HS103'}), (IP, 9999))),
                                  (0.1, (packet({'device_model': 'HS200'}), (IP, 20002))),
                                  (0.1, (packet({'device_model': 'KC401'}), (IP, 20002)))])
        self.assertEqual(self.identify(connection), {'kind': 'switch', 'model': 'HS200'})
        self.assertEqual(len(connection.sent), 2)
        self.assertEqual(len(connection.replies), 1)
        self.assertTrue(connection.closed)

    def test_late_answer_cannot_restart_total_budget(self):
        connection = TimedSocket([(1.6, (packet({'device_model': 'HS200'}), (IP, 20002)))])
        self.assertIsNone(self.identify(connection))
        self.assertEqual(connection.now, 1.5)
        self.assertTrue(connection.closed)

    def test_socket_errors_close_and_do_not_retry(self):
        for method in ('settimeout', 'sendto', 'recvfrom'):
            with self.subTest(method=method):
                connection = TimedSocket()
                setattr(connection, method, Mock(side_effect=OSError('synthetic failure')))
                self.assertIsNone(self.identify(connection))
                self.assertLessEqual(len(connection.sent), 2)
                self.assertTrue(connection.closed)
        connection = TimedSocket()
        connection.close = Mock(side_effect=OSError('synthetic close failure'))
        self.assertIsNone(self.identify(connection))
        connection.close.assert_called_once()

    def test_invalid_inputs_never_create_a_socket(self):
        factory = Mock()
        for ip in (1234, b'192.0.2.10', [], {}, '192.0.2', '192.0.2.010',
                   '192.0.2.10 ', '239.255.255.250', '255.255.255.255'):
            self.assertIsNone(identify_device(ip, socket_factory=factory))
        for timeout in (None, '1.5', 0, -1, float('inf'), float('nan')):
            self.assertIsNone(identify_device(IP, timeout=timeout, socket_factory=factory))
        factory.assert_not_called()

    def test_default_factory_creates_only_one_ipv4_udp_socket(self):
        connection = TimedSocket([(0.1, (packet({'device_type': 'SWITCH'}), (IP, 20002)))])
        with patch('app.identify.socket.socket', return_value=connection) as factory, \
                patch('app.identify.time.monotonic', side_effect=lambda: connection.now):
            self.assertEqual(identify_device(IP), {'kind': 'switch', 'model': None})
        factory.assert_called_once_with(socket.AF_INET, socket.SOCK_DGRAM)
        self.assertTrue(connection.closed)
