"""Acceptance tests for saving plugs and switches next to cameras (orchestrator-written; do
not modify). The camera list in data/cameras.json is live data and must survive every case."""

import json
import os
from pathlib import Path
import tempfile
import unittest

from app.store import MAX_DEVICES, CameraStore, StoreError

CAMERA = {"id": "cam1", "mac": "aa:bb:cc:dd:ee:01", "name": "Front"}
OLD_FILE = {"version": 1, "next_id": 2, "layout": "2x2", "cameras": [CAMERA]}


class StoreDevices(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "data" / "cameras.json"
        self.warnings = []

    def write(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    def open(self):
        return CameraStore(self.path, warning=self.warnings.append)

    def test_old_file_loads_untouched(self):
        self.write(OLD_FILE)
        before = self.path.read_bytes()
        os.utime(self.path, (1000000000, 1000000000))
        store = self.open()
        self.assertEqual(store.snapshot()["cameras"], [CAMERA])
        self.assertEqual(store.get_devices(), [])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(int(self.path.stat().st_mtime), 1000000000)
        self.assertEqual(self.warnings, [])

    def test_add_rename_remove_and_reload(self):
        self.write(OLD_FILE)
        store = self.open()
        device = store.add_device("AA-BB-CC-DD-EE-02", "Porch light")
        self.assertEqual(device, {"id": "dev1", "mac": "aa:bb:cc:dd:ee:02", "name": "Porch light"})
        second = store.add_device("aa:bb:cc:dd:ee:03", "Fan")
        self.assertEqual(second["id"], "dev2")
        store.rename_device("dev1", "Porch")
        store.remove_device("dev2")
        self.assertEqual(store.add_device("aa:bb:cc:dd:ee:03", "Fan")["id"], "dev3")
        reloaded = self.open()
        self.assertEqual(reloaded.get_devices(),
                         [{"id": "dev1", "mac": "aa:bb:cc:dd:ee:02", "name": "Porch"},
                          {"id": "dev3", "mac": "aa:bb:cc:dd:ee:03", "name": "Fan"}])
        self.assertEqual(reloaded.snapshot()["cameras"], [CAMERA])
        self.assertEqual(reloaded.snapshot()["layout"], "2x2")
        for call in (lambda: store.rename_device("dev9", "x"), lambda: store.remove_device("dev2")):
            with self.assertRaises(StoreError) as caught:
                call()
            self.assertEqual((caught.exception.code, caught.exception.status), ("not_found", 404))

    def test_a_mac_is_a_camera_or_a_device_never_both(self):
        self.write(OLD_FILE)
        store = self.open()
        with self.assertRaises(StoreError) as caught:
            store.add_device("AA:BB:CC:DD:EE:01", "Not a plug")
        self.assertEqual((caught.exception.code, caught.exception.status), ("duplicate", 409))
        store.add_device("aa:bb:cc:dd:ee:02", "Porch")
        with self.assertRaises(StoreError) as caught:
            store.add_device("aa:bb:cc:dd:ee:02", "Again")
        self.assertEqual(caught.exception.code, "duplicate")
        with self.assertRaises(StoreError) as caught:
            store.check_add("aa:bb:cc:dd:ee:02", "Camera")
        self.assertEqual((caught.exception.code, caught.exception.status), ("duplicate", 409))
        with self.assertRaises(StoreError) as caught:
            store.add_device("not a mac", "x")
        self.assertEqual(caught.exception.code, "invalid_mac")

    def test_limit(self):
        self.assertEqual(MAX_DEVICES, 6)
        self.write(OLD_FILE)
        store = self.open()
        for index in range(6):
            store.add_device("aa:bb:cc:dd:ef:%02x" % index, "Device %d" % index)
        with self.assertRaises(StoreError) as caught:
            store.add_device("aa:bb:cc:dd:ef:99", "One too many")
        self.assertEqual((caught.exception.code, caught.exception.status), ("limit", 409))

    def test_broken_devices_reset_devices_only(self):
        broken = [
            {"devices": "x", "next_device_id": 2},
            {"devices": [{"id": "dev1", "mac": "aa:bb:cc:dd:ee:02", "name": "Porch"}]},
            {"devices": [{"id": "dev1", "mac": "aa:bb:cc:dd:ee:02", "name": "Porch"}], "next_device_id": 1},
            {"devices": [{"id": "dev1", "mac": "aa:bb:cc:dd:ee:01", "name": "Camera MAC"}], "next_device_id": 2},
            {"devices": [{"id": "cam2", "mac": "aa:bb:cc:dd:ee:02", "name": "Porch"}], "next_device_id": 2},
            {"devices": [{"id": "dev1", "mac": "aa:bb:cc:dd:ee:02", "name": "Porch", "ip": "192.0.2.1"}],
             "next_device_id": 2},
            {"devices": [{"id": "dev1", "mac": "aa:bb:cc:dd:ee:02", "name": "A"},
                         {"id": "dev2", "mac": "aa:bb:cc:dd:ee:02", "name": "B"}], "next_device_id": 3},
            {"devices": [{"id": "dev%d" % n, "mac": "aa:bb:cc:dd:ef:%02x" % n, "name": "D"}
                         for n in range(1, 8)], "next_device_id": 8},
        ]
        for extra in broken:
            self.warnings.clear()
            self.write(dict(OLD_FILE, **extra))
            store = self.open()
            self.assertEqual(store.snapshot()["cameras"], [CAMERA], repr(extra)[:60])
            self.assertEqual(store.get_devices(), [], repr(extra)[:60])
            self.assertEqual(len(self.warnings), 1, repr(extra)[:60])
            self.assertFalse(list(self.path.parent.glob("*.corrupt-*")), repr(extra)[:60])

    def test_camera_rollback_keeps_a_device_saved_meanwhile(self):
        self.write(OLD_FILE)
        store = self.open()
        previous = store.snapshot()
        store.add("aa:bb:cc:dd:ee:07", "Garage")
        device = store.add_device("aa:bb:cc:dd:ee:02", "Porch")
        store.restore(previous)
        self.assertEqual(store.snapshot()["cameras"], [CAMERA])
        self.assertEqual(store.get_devices(), [device])
        self.assertEqual(self.open().get_devices(), [device])
        self.assertEqual(store.add_device("aa:bb:cc:dd:ee:03", "Fan")["id"], "dev2")


if __name__ == "__main__":
    unittest.main()
