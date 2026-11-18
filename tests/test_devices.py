"""Device discovery, polling and concurrent operations using only fake clients."""

from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, call, patch

from app.devices import DeviceService
from app.kasa_control import ControlError
from app.store import CameraStore, StoreError

MAC1, MAC2 = "aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"
IP1, IP2 = "192.0.2.10", "192.0.2.11"


class DeviceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = CameraStore(Path(directory.name) / "data/cameras.json")
        self.client = Mock()
        self.client.read.return_value = False
        self.client.set.return_value = True
        self.lookup, self.sweep = Mock(return_value=IP1), Mock()
        self.identify = Mock(return_value={"kind": "plug", "model": "HS103"})
        self.now = 100.0
        self.service = DeviceService(self.store, self.client, self.lookup, self.sweep,
                                     self.identify, lambda: self.now)
        self.addCleanup(self.service.stop)

    def assert_error(self, status, code, action, *args):
        with self.assertRaises(StoreError) as caught:
            action(*args)
        self.assertEqual((caught.exception.status, caught.exception.code), (status, code))
        self.assertNotIn("PRIVATE", caught.exception.message)
        self.assertNotIn(IP1, caught.exception.message)
        return caught.exception.message

    def test_add_returns_public_fields_and_saves_only_identity(self):
        self.assertEqual(self.service.settings(), {"devices": [], "max": 6})
        for index, kind in enumerate(("plug", "switch", "plug_or_switch"), 1):
            self.identify.return_value = {"kind": kind}
            mac = "aa:bb:cc:dd:ee:%02x" % index
            result = self.service.add({"mac": mac.upper(), "name": " Light "})
            self.assertEqual(result, {"id": "dev%d" % index, "name": "Light", "mac": mac, "state": "off"})
        self.assertTrue(all(set(device) == {"id", "mac", "name"} for device in self.store.get_devices()))
        self.assertEqual(len(self.service.settings()["devices"]), 3)
        self.sweep.assert_not_called()
        self.assertNotIn(IP1, str(self.service.settings()))

    def test_validation_precedes_discovery(self):
        for body, code in (({}, "invalid_mac"), ({"mac": "bad"}, "invalid_mac"),
                           ({"mac": MAC1, "name": "\n"}, "invalid_name"),
                           ([], "invalid_device"), ({"mac": MAC1, "ip": IP1}, "invalid_device")):
            self.assert_error(400, code, self.service.add, body)
        self.lookup.assert_not_called()
        self.sweep.assert_not_called()
        self.identify.assert_not_called()
        self.client.read.assert_not_called()

    def test_missing_ip_sweeps_once_and_looks_up_again(self):
        self.lookup.side_effect = [None, IP1]
        self.assertEqual(self.service.add({"mac": MAC1})["state"], "off")
        self.assertEqual(self.lookup.call_args_list, [call(MAC1), call(MAC1)])
        self.sweep.assert_called_once_with()
        self.identify.assert_called_once_with(IP1)
        self.client.read.assert_called_once_with(IP1)

    def test_missing_device_and_discovery_errors_have_fixed_messages(self):
        for lookup in (Mock(return_value=None), Mock(return_value="invalid"),
                       Mock(side_effect=OSError("PRIVATE"))):
            self.service.lookup = lookup
            self.sweep.side_effect = OSError("PRIVATE")
            message = self.assert_error(404, "not_found", self.service.add, {"mac": MAC1})
            self.assertEqual(message, "Device not found on this network.")
        self.identify.assert_not_called()
        self.assertEqual(self.store.get_devices(), [])

    def test_non_devices_are_identified_without_reading_or_saving(self):
        cases = [(None, "This is not a Kasa plug or switch."),
                 ({"kind": "unknown"}, "This is not a Kasa plug or switch."),
                 ({"kind": "camera", "model": "KC200"}, "This is a camera. Add it under Cameras."),
                 ({"kind": "router", "model": "X55"},
                  "This is a TP-Link Wi-Fi router (X55), not a plug or switch."),
                 ({"kind": "light", "model": "KL100"},
                  "This is a Kasa smart light (KL100), not a plug or switch."),
                 ({"kind": "other", "model": "<PRIVATE>"},
                  "This is a TP-Link device, not a plug or switch.")]
        for identity, message in cases:
            self.identify.return_value = identity
            self.assertEqual(self.assert_error(422, "not_device", self.service.add, {"mac": MAC1}), message)
        self.identify.side_effect = ValueError("PRIVATE")
        self.assert_error(422, "not_device", self.service.add, {"mac": MAC1})
        self.client.read.assert_not_called()
        self.assertEqual(self.store.get_devices(), [])

    def test_add_read_errors_are_sanitized_and_do_not_save(self):
        for failure, status, code, message in (
                (ControlError("auth"), 422, "auth", "The device rejected the Kasa account login."),
                (ControlError("unsupported"), 422, "not_device", "Multi-outlet devices are not supported yet."),
                (ControlError("protocol"), 502, "unreachable", "The device did not answer."),
                (ControlError("unreachable"), 502, "unreachable", "The device did not answer."),
                (RuntimeError("PRIVATE"), 502, "unreachable", "The device did not answer.")):
            self.client.read.side_effect = failure
            self.assertEqual(self.assert_error(status, code, self.service.add, {"mac": MAC1}), message)
            self.assertEqual(self.store.get_devices(), [])

    def test_set_requires_exact_boolean_body_and_an_existing_device(self):
        self.store.add_device(MAC1, "Light")
        for body in (None, [], {}, {"on": 0}, {"on": 1}, {"on": "true"}, {"on": None},
                     {"on": True, "ip": IP1}):
            self.assert_error(400, "invalid_state", self.service.set_state, "dev1", body)
        self.assert_error(404, "not_found", self.service.set_state, "dev9", {"on": True})
        self.lookup.assert_not_called()
        self.client.set.assert_not_called()

    def test_set_uses_cached_ip_and_returns_the_reported_state_without_mac(self):
        self.service.add({"mac": MAC1, "name": "Light"})
        self.lookup.reset_mock()
        self.client.set.return_value = False
        self.assertEqual(self.service.set_state("dev1", {"on": True}),
                         {"id": "dev1", "name": "Light", "state": "off"})
        self.client.set.assert_called_once_with(IP1, True)
        self.lookup.assert_not_called()
        self.client.set.return_value = True
        self.assertEqual(self.service.set_state("dev1", {"on": False})["state"], "on")

    def test_set_failure_marks_unknown_and_refreshes_ip_without_replaying(self):
        self.service.add({"mac": MAC1})
        self.lookup.return_value = IP2
        for failure, status, code in ((ControlError("auth"), 422, "auth"),
                                      (ControlError("protocol"), 502, "unreachable"),
                                      (OSError("PRIVATE"), 502, "unreachable")):
            self.client.set.reset_mock()
            self.client.set.side_effect = failure
            self.assert_error(status, code, self.service.set_state, "dev1", {"on": True})
            self.client.set.assert_called_once()
            self.assertEqual(self.service.settings()["devices"][0]["state"], "unknown")
        self.client.set.side_effect = None
        self.client.set.return_value = False
        self.assertEqual(self.service.set_state("dev1", {"on": False})["state"], "off")
        self.assertEqual(self.client.set.call_args, call(IP2, False))

    def test_missing_ip_and_non_boolean_client_results_mark_unknown(self):
        self.store.add_device(MAC1, "Light")
        self.lookup.return_value = None
        self.assert_error(502, "unreachable", self.service.set_state, "dev1", {"on": True})
        self.client.set.assert_not_called()
        self.lookup.return_value = IP1
        self.client.set.return_value = 1
        self.assert_error(502, "unreachable", self.service.set_state, "dev1", {"on": True})
        self.assertEqual(self.service.settings()["devices"][0]["state"], "unknown")

    def test_poll_intervals_retry_unknown_and_refresh_ip_without_sweeps(self):
        self.store.add_device(MAC1, "Light")
        self.client.read.side_effect = [ControlError("unreachable"), True, False]
        self.lookup.side_effect = [IP1, IP2]
        self.service.refresh_once()
        self.assertEqual(self.service.settings()["devices"][0]["state"], "unknown")
        self.now += 2.9
        self.service.refresh_once()
        self.assertEqual(self.client.read.call_count, 1)
        self.now = 103
        self.service.refresh_once()
        self.assertEqual(self.client.read.call_args, call(IP2))
        self.assertEqual(self.service.settings()["devices"][0]["state"], "on")
        self.now = 117.9
        self.service.refresh_once()
        self.assertEqual(self.client.read.call_count, 2)
        self.now = 118
        self.service.refresh_once()
        self.assertEqual(self.service.settings()["devices"][0]["state"], "off")
        self.sweep.assert_not_called()

    def test_polling_one_failed_device_does_not_skip_healthy_devices(self):
        self.store.add_device(MAC1, "Light")
        self.store.add_device(MAC2, "Fan")
        self.lookup.side_effect = lambda mac: IP1 if mac == MAC1 else IP2
        self.client.read.side_effect = [ControlError("auth"), True, False]
        self.service.refresh_once()
        self.assertEqual([device["state"] for device in self.service.settings()["devices"]], ["unknown", "on"])
        self.now += 3
        self.service.refresh_once()
        self.assertEqual(self.client.read.call_args_list, [call(IP1), call(IP2), call(IP1)])

    def test_rename_remove_and_failed_save_preserve_cache_consistency(self):
        self.service.add({"mac": MAC1, "name": "Light"})
        self.assertEqual(self.service.rename("dev1", {"name": " Porch "})["name"], "Porch")
        self.assert_error(400, "invalid_name", self.service.rename, "dev1", {"name": ""})
        self.assert_error(400, "invalid_name", self.service.rename, "dev1", {})
        with patch.object(self.store, "_save", side_effect=OSError("PRIVATE")):
            with self.assertRaises(OSError):
                self.service.remove("dev1")
        self.assertEqual(self.service.settings()["devices"][0]["state"], "off")
        self.service.remove("dev1")
        self.assertEqual(self.service.settings()["devices"], [])
        self.assert_error(404, "not_found", self.service.remove, "dev1")
        self.client.set.assert_not_called()

    def test_settings_and_rename_do_not_wait_for_network_and_remove_does_not_resurrect(self):
        self.store.add_device(MAC1, "Light")
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()

        def read(ip):
            entered.set()
            if not release.wait(2):
                raise AssertionError("Test did not release fake device")
            return True

        self.client.read.side_effect = read
        worker = threading.Thread(target=self.service.refresh_once, daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))

            def settings():
                self.service.rename("dev1", {"name": "Porch"})
                self.service.settings()
                self.service.remove("dev1")
                finished.set()

            observer = threading.Thread(target=settings, daemon=True)
            observer.start()
            self.assertTrue(finished.wait(1), "Cached operations waited for the network")
            observer.join(1)
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.service.settings()["devices"], [])
        self.assertEqual(self.service._states, {})
        self.assertEqual(self.service._ips, {})

    def test_queued_button_runs_before_the_next_background_device(self):
        self.store.add_device(MAC1, "Light")
        self.store.add_device(MAC2, "Fan")
        self.lookup.side_effect = lambda mac: IP1 if mac == MAC1 else IP2
        entered, release, queued = threading.Event(), threading.Event(), threading.Event()
        order, errors = [], []

        def read(ip):
            order.append(("read", ip))
            if ip == IP1:
                entered.set()
                release.wait(2)
            return False

        self.client.read.side_effect = read
        self.client.set.side_effect = lambda ip, on: order.append(("set", ip)) or True
        original = self.service._operation_lock

        class ObservedLock:
            def __enter__(inner):
                if threading.current_thread().name == "button-test":
                    queued.set()
                original.acquire()

            def __exit__(inner, *args):
                original.release()

        self.service._operation_lock = ObservedLock()
        poller = threading.Thread(target=self.service.refresh_once, daemon=True)

        def set_state():
            try:
                self.service.set_state("dev2", {"on": True})
            except Exception as error:
                errors.append(error)

        button = threading.Thread(target=set_state, name="button-test", daemon=True)
        poller.start()
        try:
            self.assertTrue(entered.wait(1))
            button.start()
            self.assertTrue(queued.wait(1))
        finally:
            release.set()
            poller.join(2)
            button.join(2)
        self.assertFalse(poller.is_alive() or button.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(order, [("read", IP1), ("set", IP2)])

    def test_start_stop_are_idempotent_and_initial_poll_is_read_only(self):
        self.store.add_device(MAC1, "Light")
        read = threading.Event()
        self.client.read.side_effect = lambda ip: read.set() or False
        self.service.start()
        worker = self.service._thread
        self.service.start()
        self.assertIs(self.service._thread, worker)
        self.assertTrue(read.wait(1))
        self.service.stop()
        self.service.stop()
        self.assertFalse(worker.is_alive())
        self.client.set.assert_not_called()


if __name__ == "__main__":
    unittest.main()
