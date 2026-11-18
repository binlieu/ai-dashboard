import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.discovery import ip_for_mac, normalize_mac, parse_arp, resolve_cameras, sweep_local_network, valid_ip

ARP = '''? (192.168.1.1) at 0:1:2:3:4:5 on en1 ifscope [ethernet]
? (192.168.1.202) at (incomplete) on en1 ifscope [ethernet]
? (192.168.1.178) at aa:bb:cc:dd:ee:01 on en1 ifscope [ethernet]
? (192.168.1.180) at c:70:43:ad:c0:b6 on en1 ifscope [ethernet]'''


class DiscoveryTests(unittest.TestCase):
    def test_normalize_mac(self):
        for value, expected in [('AA:BB:CC:DD:EE:01', 'aa:bb:cc:dd:ee:01'),
                                 ('AA-BB-CC-DD-EE-01', 'aa:bb:cc:dd:ee:01'),
                                 ('0C:70:43:AD:C0:B6', '0c:70:43:ad:c0:b6'),
                                 ('00:01:02:0A:0F:00', '00:01:02:0a:0f:00'),
                                 ('c:0:1:a:b:ff', '0c:00:01:0a:0b:ff')]:
            with self.subTest(value=value):
                self.assertEqual(normalize_mac(value), expected)

    def test_invalid_mac(self):
        for value in ('74:GG:CE:DD:EE:01', 'aa:bb:cc:7c:98', '0aa:bb:cc:dd:ee:01',
                      'aa-bb:cc:dd:ee:01', '', None, 4, '1:2:3:4:5:6\n'):
            with self.subTest(value=value):
                self.assertIsNone(normalize_mac(value))

    def test_arp_parsing_and_matching(self):
        self.assertEqual(len(parse_arp(ARP)), 3)
        self.assertEqual(ip_for_mac('AA:BB:CC:DD:EE:01', ARP), '192.168.1.178')
        self.assertEqual(ip_for_mac('0C-70-43-AD-C0-B6', ARP), '192.168.1.180')
        self.assertIsNone(ip_for_mac('aa:bb:cc:dd:ee:ff', ARP))
        self.assertIsNone(ip_for_mac('invalid', ARP))
        self.assertEqual(ip_for_mac('c:70:43:ad:c0:b6', '? (192.168.1.181) at 0C:70:43:AD:C0:B6 on en1'), '192.168.1.181')
        self.assertEqual(parse_arp('? (999.1.1.1) at 1:2:3:4:5:6 on en1'), {})

    @patch('app.discovery.subprocess.run')
    def test_system_arp_and_empty_explicit_table(self, run):
        run.return_value = SimpleNamespace(stdout=ARP, returncode=0)
        self.assertEqual(ip_for_mac('aa:bb:cc:dd:ee:01'), '192.168.1.178')
        self.assertEqual(run.call_args.args[0], ['/usr/sbin/arp', '-an'])
        run.reset_mock()
        self.assertIsNone(ip_for_mac('aa:bb:cc:dd:ee:01', ''))
        run.assert_not_called()

    @patch('app.discovery.subprocess.run', side_effect=OSError)
    def test_arp_failure(self, run):
        self.assertIsNone(ip_for_mac('aa:bb:cc:dd:ee:01'))

    def test_sweep_is_one_parallel_bounded_pass(self):
        child = Mock()
        child.poll.return_value = 0
        answers = [SimpleNamespace(stdout=' interface: en7\n', returncode=0),
                   SimpleNamespace(stdout='192.0.2.44\n', returncode=0)]
        with patch('app.discovery.subprocess.run', side_effect=answers) as run, \
                patch('app.discovery.subprocess.Popen', return_value=child) as popen, \
                patch('app.discovery.time.monotonic', return_value=0):
            sweep_local_network()
        self.assertEqual(run.call_args_list[0].args[0], ['/sbin/route', '-n', 'get', 'default'])
        self.assertEqual(run.call_args_list[1].args[0], ['/usr/sbin/ipconfig', 'getifaddr', 'en7'])
        self.assertEqual(popen.call_count, 254)
        self.assertEqual(popen.call_args_list[-1].args[0], ['/sbin/ping', '-c', '1', '-t', '1', '192.0.2.254'])
        self.assertTrue(all('shell' not in call.kwargs for call in popen.call_args_list))

    def test_sweep_deadline_kills_children(self):
        child = Mock()
        child.poll.return_value = None
        child.wait.side_effect = subprocess.TimeoutExpired('ping', 0)
        with patch('app.discovery.subprocess.run', side_effect=[
                SimpleNamespace(stdout='interface: en0\n', returncode=0),
                SimpleNamespace(stdout='192.0.2.44', returncode=0)]), \
                patch('app.discovery.subprocess.Popen', return_value=child) as popen, \
                patch('app.discovery.time.monotonic', side_effect=range(1000)):
            sweep_local_network()
        self.assertLess(popen.call_count, 254)
        self.assertTrue(child.kill.called)

    def test_resolve_retries_only_missing(self):
        cameras = [{'id': 'cam1', 'mac': 'a'}, {'id': 'cam2', 'mac': 'b'}]
        lookup = Mock(side_effect=['192.0.2.1', None, '192.0.2.2'])
        sweep = Mock()
        resolved = resolve_cameras(cameras, lookup, sweep)
        self.assertEqual([camera['ip'] for camera in resolved], ['192.0.2.1', '192.0.2.2'])
        self.assertEqual([call.args[0] for call in lookup.call_args_list], ['a', 'b', 'b'])
        sweep.assert_called_once_with()
        self.assertNotIn('ip', cameras[0])

    def test_invalid_ip(self):
        for value in (None, 42, '127.0.0.1;touch x', '999.0.0.1', '::1'):
            self.assertIsNone(valid_ip(value))
