"""Acceptance tests for camera backup recording (orchestrator-written; do not modify).

The clean-up deletes files on the user's drive, so these tests pin exactly what it may touch:
only its own segment files, inside a folder it has marked, for cameras that are being backed up.
"""

import datetime
import os
from pathlib import Path
import struct
import tempfile
import unittest

from app.backup import (DAYS, MARKER, MIN_FREE_BYTES, SEGMENT_SECONDS, BackupError, prune,
                        segment_path, split_boxes, validate_folder)
from app.security import check_request
from app.store import CameraStore

NOW = datetime.datetime(2026, 11, 18, 12, 0, 0)
PLENTY = lambda: 10 ** 12


def box(kind, size):
    return struct.pack(">I4s", size, kind) + bytes(size - 8)


class Constants(unittest.TestCase):
    def test_values(self):
        self.assertEqual(DAYS, (1, 7, 30))
        self.assertEqual(SEGMENT_SECONDS, 180)
        self.assertEqual(MARKER, ".kasa-dashboard-backup")
        self.assertEqual(MIN_FREE_BYTES, 5 * 1024 ** 3)


class SplitBoxes(unittest.TestCase):
    def test_counts_only_whole_top_level_boxes(self):
        data = box(b"ftyp", 24) + box(b"moov", 100) + box(b"moof", 40) + box(b"mdat", 300)
        self.assertEqual(split_boxes(data), len(data))
        self.assertEqual(split_boxes(data[:-1]), 24 + 100 + 40)
        self.assertEqual(split_boxes(data + b"\x00\x00\x01"), len(data))
        self.assertEqual(split_boxes(data + box(b"moof", 40)[:12]), len(data))
        self.assertEqual(split_boxes(b""), 0)
        self.assertEqual(split_boxes(box(b"ftyp", 24)[:7]), 0)

    def test_large_size_form(self):
        big = struct.pack(">I4sQ", 1, b"mdat", 32) + bytes(16)
        self.assertEqual(split_boxes(big + box(b"moof", 16)), 48)
        self.assertEqual(split_boxes(big[:20]), 0)

    def test_bad_streams_are_refused(self):
        bad = [struct.pack(">I4s", 0, b"mdat") + bytes(8), struct.pack(">I4s", 4, b"mdat"),
               struct.pack(">I4s", 7, b"free") + bytes(8), struct.pack(">I4sQ", 1, b"mdat", 8),
               struct.pack(">I4s", 65 * 1024 * 1024, b"mdat"), box(b"ftyp", 24) + b"\x00\x00\x00\x03abcd",
               struct.pack(">I4s", 16, b"\x00\x01\x02\x03") + bytes(8)]
        for data in bad:
            with self.assertRaises(BackupError, msg=data[:12]):
                split_boxes(data)


class SegmentPath(unittest.TestCase):
    def test_layout(self):
        when = datetime.datetime(2026, 11, 8, 7, 3, 9)
        self.assertEqual(segment_path("/Volumes/Drive/cams", "cam4", when),
                         Path("/Volumes/Drive/cams/cam4/2026-11-08/07-03-09.mp4"))

    def test_only_camera_ids(self):
        for bad in ("../x", "cam0", "dev1", "cam1/..", "", None, "CAM1"):
            with self.assertRaises(BackupError):
                segment_path("/tmp/x", bad, NOW)


class ValidateFolder(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()

    def code(self, text, forbidden=()):
        with self.assertRaises(BackupError) as caught:
            validate_folder(text, forbidden)
        return caught.exception.code

    def test_accepts_an_existing_writable_directory(self):
        target = self.base / "Camera backups"
        target.mkdir()
        self.assertEqual(validate_folder(str(target)), str(target))
        self.assertEqual(validate_folder(str(target) + "/"), str(target))
        self.assertEqual(validate_folder(""), "")

    def test_rejects_unusable_text(self):
        for text in (None, 5, "relative/path", "~/Movies", "x" * 2000, str(self.base) + "\n",
                     str(self.base) + "\x00", "/"):
            self.assertEqual(self.code(text), "invalid_folder", repr(text)[:30])

    def test_must_exist_and_is_never_created(self):
        missing = self.base / "not-mounted" / "cams"
        self.assertEqual(self.code(str(missing)), "folder_missing")
        self.assertFalse(missing.exists())
        self.assertFalse(missing.parent.exists())
        plain = self.base / "file.txt"
        plain.write_text("x")
        self.assertEqual(self.code(str(plain)), "folder_missing")

    def test_not_inside_or_around_the_project(self):
        project = self.base / "project"
        (project / "www").mkdir(parents=True)
        outside = self.base / "outside"
        outside.mkdir()
        self.assertEqual(self.code(str(project), [str(project)]), "folder_forbidden")
        self.assertEqual(self.code(str(project / "www"), [str(project)]), "folder_forbidden")
        self.assertEqual(self.code(str(self.base), [str(project)]), "folder_forbidden")
        link = self.base / "link"
        link.symlink_to(project / "www")
        self.assertEqual(self.code(str(link), [str(project)]), "folder_forbidden")
        self.assertEqual(validate_folder(str(outside), [str(project)]), str(outside))

    def test_read_only(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory modes")
        target = self.base / "ro"
        target.mkdir()
        target.chmod(0o500)
        self.addCleanup(target.chmod, 0o700)
        self.assertEqual(self.code(str(target)), "folder_readonly")


class Prune(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve() / "backups"
        self.root.mkdir()
        (self.root / MARKER).write_text("")

    def make(self, relative, content=b"x"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def left(self):
        return sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*") if path.is_file())

    def test_age_comes_from_the_name_and_each_camera_has_its_own_period(self):
        keep = [self.make("cam1/2026-11-17/12-00-00.mp4"), self.make("cam1/2026-11-18/11-57-00.mp4"),
                self.make("cam4/2026-11-11/12-00-00.mp4"), self.make("cam4/2026-11-17/11-00-00.mp4"),
                self.make("cam6/2026-10-19/12-00-00.mp4")]
        gone = [self.make("cam1/2026-11-17/11-59-59.mp4"), self.make("cam1/2026-11-10/08-00-00.mp4"),
                self.make("cam4/2026-11-11/11-59-59.mp4"), self.make("cam6/2026-10-19/11-59-59.mp4"),
                self.make("cam6/2025-01-01/00-00-00.mp4")]
        for path in keep + gone:
            os.utime(path, (0, 0))
        removed = prune(self.root, {"cam1": 1, "cam4": 7, "cam6": 30}, NOW, PLENTY, MIN_FREE_BYTES)
        self.assertEqual(sorted(removed), sorted(gone))
        for path in keep:
            self.assertTrue(path.exists(), path)
        self.assertFalse((self.root / "cam1/2026-11-10").exists())
        self.assertFalse((self.root / "cam6/2025-01-01").exists())
        self.assertTrue((self.root / "cam1").is_dir())
        self.assertTrue((self.root / MARKER).exists())

    def test_nothing_happens_without_the_marker(self):
        old = self.make("cam1/2020-01-01/00-00-00.mp4")
        (self.root / MARKER).unlink()
        self.assertEqual(prune(self.root, {"cam1": 1}, NOW, lambda: 0, MIN_FREE_BYTES), [])
        self.assertTrue(old.exists())
        self.assertEqual(prune(self.root / "missing", {"cam1": 1}, NOW, PLENTY, MIN_FREE_BYTES), [])

    def test_everything_that_is_not_its_own_segment_is_left_alone(self):
        untouched = ["cam2/2020-01-01/00-00-00.mp4", "cam1/2020-01-01/notes.txt",
                     "cam1/2020-01-01/00-00-00.mp4.bak", "cam1/2020-01-01/0-0-0.mp4",
                     "cam1/2020-01-01/25-00-00.mp4", "cam1/2020-13-01/00-00-00.mp4",
                     "cam1/old/00-00-00.mp4", "cam1/2020-01-01/sub/00-00-00.mp4",
                     "cam1/00-00-00.mp4", "00-00-00.mp4", "holiday.mp4", "dev1/2020-01-01/00-00-00.mp4",
                     "Cam1x/2020-01-01/00-00-00.mp4", "cam1/2020-01-01/00-00-00.MP4",
                     "cam1/camera-name.txt"]
        for relative in untouched:
            self.make(relative)
        outside = self.root.parent / "outside"
        self.make("../outside/2020-01-01/00-00-00.mp4")
        (self.root / "cam1" / "2020-02-02").symlink_to(outside / "2020-01-01", target_is_directory=True)
        (self.root / "cam7").symlink_to(outside, target_is_directory=True)
        self.make("cam1/2020-03-03/placeholder.txt")
        (self.root / "cam1/2020-03-03/00-00-00.mp4").symlink_to(outside / "2020-01-01/00-00-00.mp4")
        removed = prune(self.root, {"cam1": 1, "cam7": 1}, NOW, lambda: 0, MIN_FREE_BYTES)
        self.assertEqual(removed, [])
        for relative in untouched:
            self.assertTrue((self.root / relative).exists(), relative)
        self.assertTrue((outside / "2020-01-01/00-00-00.mp4").exists())
        self.assertTrue((self.root / "cam1/2020-03-03/00-00-00.mp4").is_symlink())

    def test_low_space_removes_the_oldest_first_across_cameras_until_enough(self):
        files = [self.make("cam1/2026-11-15/09-00-00.mp4"), self.make("cam4/2026-11-15/09-30-00.mp4"),
                 self.make("cam1/2026-11-16/10-00-00.mp4"), self.make("cam4/2026-11-16/10-30-00.mp4"),
                 self.make("cam4/2026-11-17/11-00-00.mp4")]
        free = [MIN_FREE_BYTES - 1, MIN_FREE_BYTES - 1, MIN_FREE_BYTES]
        calls = []

        def free_space():
            calls.append(1)
            return free[min(len(calls) - 1, 2)]
        removed = prune(self.root, {"cam1": 30, "cam4": 30}, NOW, free_space, MIN_FREE_BYTES)
        self.assertEqual(removed, files[:2])
        self.assertEqual([path.exists() for path in files], [False, False, True, True, True])

    def test_low_space_never_touches_the_last_24_hours(self):
        old = [self.make("cam1/2026-11-16/08-00-00.mp4"), self.make("cam4/2026-11-17/11-59-59.mp4")]
        last_day = [self.make("cam4/2026-11-17/12-00-00.mp4"), self.make("cam1/2026-11-17/18-00-00.mp4"),
                    self.make("cam1/2026-11-18/11-54-00.mp4")]
        removed = prune(self.root, {"cam1": 30, "cam4": 7}, NOW, lambda: 0, MIN_FREE_BYTES)
        self.assertEqual(removed, old)
        for path in last_day:
            self.assertTrue(path.exists(), path)
        self.assertEqual(prune(self.root, {"cam1": 30, "cam4": 7}, NOW, lambda: 0, MIN_FREE_BYTES), [])

    def test_low_space_never_removes_files_in_use(self):
        active = self.make("cam1/2026-11-18/11-57-00.mp4.part")
        held = self.make("cam1/2026-11-10/11-54-00.mp4")
        older = self.make("cam1/2026-11-09/11-54-00.mp4")
        removed = prune(self.root, {"cam1": 30}, NOW, lambda: 0, MIN_FREE_BYTES, active=[active, held])
        self.assertEqual(removed, [older])
        self.assertTrue(active.exists() and held.exists())

    def test_abandoned_partial_files_are_cleared_after_an_hour(self):
        stale = self.make("cam1/2026-11-18/10-59-59.mp4.part")
        recent = self.make("cam1/2026-11-18/11-30-00.mp4.part")
        in_use = self.make("cam1/2026-11-18/09-00-00.mp4.part")
        removed = prune(self.root, {"cam1": 30}, NOW, PLENTY, MIN_FREE_BYTES, active=[in_use])
        self.assertEqual(removed, [stale])
        self.assertTrue(recent.exists() and in_use.exists())

    def test_bad_policy_values_do_nothing(self):
        old = self.make("cam1/2020-01-01/00-00-00.mp4")
        for policies in ({"cam1": 0}, {"cam1": -1}, {"cam1": True}, {"cam1": "7"}, {"cam1": 2},
                         {"../cam1": 1}, None):
            self.assertEqual(prune(self.root, policies, NOW, PLENTY, MIN_FREE_BYTES), [], repr(policies))
        self.assertTrue(old.exists())


class Gate(unittest.TestCase):
    ORIGIN = "http://127.0.0.1:1984"

    def h(self, **extra):
        headers = {"host": "127.0.0.1:1984"}
        headers.update({key.replace("_", "-"): value for key, value in extra.items()})
        return headers

    def test_backup_route(self):
        ok = self.h(origin=self.ORIGIN, content_type="application/json")
        self.assertEqual(check_request("GET", "/api/backup", self.h(), 1984), 200)
        self.assertEqual(check_request("PUT", "/api/backup", ok, 1984), 200)
        self.assertEqual(check_request("PUT", "/api/backup", self.h(content_type="application/json"), 1984), 403)
        self.assertEqual(check_request("PUT", "/api/backup", self.h(origin="http://evil.example",
                                                                    content_type="application/json"), 1984), 403)
        self.assertEqual(check_request("PUT", "/api/backup", self.h(origin=self.ORIGIN), 1984), 415)
        self.assertEqual(check_request("GET", "/api/backup", self.h(origin="http://evil.example"), 1984), 403)
        for method in ("POST", "DELETE", "PATCH"):
            self.assertEqual(check_request(method, "/api/backup", ok, 1984), 405)
        for target in ("/api/backup/", "/api/backup?folder=/tmp", "/api/backup/cam1", "/api/backups",
                       "/api/stream.mp4?src=cam1", "/api/backup/files"):
            self.assertIn(check_request("GET", target, ok, 1984), (403, 404), target)


class StoreBackup(unittest.TestCase):
    CAMERAS = [{"id": "cam1", "mac": "aa:bb:cc:dd:ee:01", "name": "Front"},
               {"id": "cam4", "mac": "aa:bb:cc:dd:ee:04", "name": "Yard"}]

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "data" / "cameras.json"
        self.path.parent.mkdir()
        self.warnings = []

    def write(self, **extra):
        import json
        state = {"version": 1, "next_id": 5, "layout": "auto", "cameras": self.CAMERAS}
        state.update(extra)
        self.path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    def open(self):
        return CameraStore(self.path, warning=self.warnings.append)

    def test_old_file_loads_untouched_with_backup_off(self):
        self.write()
        before = self.path.read_bytes()
        store = self.open()
        self.assertEqual(store.get_backup(), {"folder": "", "cameras": {}})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.warnings, [])

    def test_save_reload_and_camera_removal(self):
        self.write()
        store = self.open()
        saved = store.set_backup({"folder": "/Volumes/Drive/cams", "cameras": {"cam1": 7, "cam4": 30}})
        self.assertEqual(saved, {"folder": "/Volumes/Drive/cams", "cameras": {"cam1": 7, "cam4": 30}})
        self.assertEqual(self.open().get_backup(), saved)
        store.remove("cam4")
        self.assertEqual(store.get_backup()["cameras"], {"cam1": 7})
        self.assertEqual(len(store.snapshot()["cameras"]), 1)

    def test_invalid_settings_are_refused_and_nothing_changes(self):
        self.write()
        store = self.open()
        store.set_backup({"folder": "/Volumes/Drive/cams", "cameras": {"cam1": 7}})
        for bad in (None, {}, {"folder": "/x"}, {"folder": 5, "cameras": {}}, {"folder": "relative", "cameras": {}},
                    {"folder": "/x", "cameras": {"cam9": 7}}, {"folder": "/x", "cameras": {"cam1": 2}},
                    {"folder": "/x", "cameras": {"cam1": True}}, {"folder": "/x", "cameras": {"cam1": "7"}},
                    {"folder": "/x", "cameras": [["cam1", 7]]}, {"folder": "", "cameras": {"cam1": 7}},
                    {"folder": "/x", "cameras": {}, "extra": 1}, {"folder": "/x\n", "cameras": {}}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                store.set_backup(bad)
        self.assertEqual(store.get_backup(), {"folder": "/Volumes/Drive/cams", "cameras": {"cam1": 7}})

    def test_broken_backup_in_the_file_resets_backup_only(self):
        for broken in ("x", {"folder": "/x"}, {"folder": "/x", "cameras": {"cam9": 7}},
                       {"folder": "/x", "cameras": {"cam1": 3}}):
            self.warnings.clear()
            self.write(backup=broken)
            store = self.open()
            self.assertEqual(store.snapshot()["cameras"], self.CAMERAS, repr(broken))
            self.assertEqual(store.get_backup(), {"folder": "", "cameras": {}}, repr(broken))
            self.assertEqual(len(self.warnings), 1, repr(broken))

    def test_camera_rollback_keeps_a_backup_save_made_meanwhile(self):
        self.write()
        store = self.open()
        previous = store.snapshot()
        store.add("aa:bb:cc:dd:ee:09", "Garage")
        saved = store.set_backup({"folder": "/Volumes/Drive/cams", "cameras": {"cam1": 1}})
        store.restore(previous)
        self.assertEqual(store.get_backup(), saved)
        self.assertEqual(len(store.snapshot()["cameras"]), 2)


if __name__ == "__main__":
    unittest.main()
