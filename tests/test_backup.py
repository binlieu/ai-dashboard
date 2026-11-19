import datetime
import os
from pathlib import Path
import queue
import stat
import struct
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from app import backup
from app.backup import BackupError, BackupService, MARKER, MIN_FREE_BYTES, prune, segment_path
from app.store import CameraStore

NOW = datetime.datetime(2026, 11, 18, 12)
PLENTY = MIN_FREE_BYTES * 2


def box(kind, payload=b''):
    return struct.pack('>I4s', 8 + len(payload), kind) + payload


HEADER = box(b'ftyp') + box(b'moov')
MOOF = box(b'moof')
VIDEO = box(b'mdat', b'placeholder')
PAIR = MOOF + VIDEO


class FakeStream:
    def __init__(self):
        self.chunks = queue.Queue()
        self.closed = threading.Event()

    def read(self, size):
        return self.chunks.get(timeout=2)

    def feed(self, chunk):
        self.chunks.put(chunk)

    def interrupt(self):
        self.feed(b'')

    def close(self):
        self.closed.set()


class BackupTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()
        self.root = self.base / 'backup'
        self.root.mkdir()
        self.project = self.base / 'project'
        self.store = CameraStore(self.project / 'state.json', {
            'CAMERA_MACS': 'aa:bb:cc:dd:ee:01, aa:bb:cc:dd:ee:02',
            'CAMERA_NAMES': 'Front, Yard',
        })
        self.sources, self.opened = queue.Queue(), queue.Queue()
        self.free_space = Mock(return_value=PLENTY)
        self.elapsed = 0

    def make_service(self, **kwargs):
        def open_stream(camera_id):
            self.opened.put(camera_id)
            return self.sources.get_nowait()

        service = BackupService(self.store, open_stream, (self.project,), clock=lambda: self.elapsed,
                                now=lambda: NOW, free_space=self.free_space, **kwargs)
        self.addCleanup(service.stop)
        return service

    def configure(self, service, cameras=None, folder=None):
        return service.configure({'folder': str(self.root if folder is None else folder),
                                  'cameras': {'cam1': 7} if cameras is None else cameras})

    def watch_pairs(self):
        written = queue.Queue()
        original = backup._Segment.box

        def write(segment, data):
            complete = original(segment, data)
            if complete:
                written.put(segment.path)
            return complete

        patcher = patch.object(backup._Segment, 'box', write)
        patcher.start()
        self.addCleanup(patcher.stop)
        return written

    def test_configure_marks_only_valid_folders_and_preserves_existing_readme(self):
        service = self.make_service()
        readme = self.root / 'README.txt'
        readme.write_text('Keep this file.')
        for body in ({}, {'folder': str(self.root), 'cameras': {'cam8': 1}},
                     {'folder': str(self.root), 'cameras': {'cam2': True}},
                     {'folder': '', 'cameras': {'cam1': 7}}):
            with self.assertRaises(BackupError) as caught:
                service.configure(body)
            self.assertEqual(caught.exception.code, 'invalid_settings')
        self.assertFalse((self.root / MARKER).exists())
        with self.assertRaises(BackupError) as caught:
            self.configure(service, folder=self.project)
        self.assertEqual(caught.exception.code, 'folder_forbidden')

        status = self.configure(service, {'cam1': 7, 'cam2': 0})
        self.assertIsNone(status['free_bytes'])
        self.assertEqual([(cam['days'], cam['state']) for cam in status['cameras']], [(7, 'waiting'), (0, 'off')])
        self.assertEqual(self.store.get_backup(), {'folder': str(self.root), 'cameras': {'cam1': 7}})
        self.assertTrue((self.root / MARKER).is_file())
        self.assertEqual(readme.read_text(), 'Keep this file.')
        self.assertEqual(self.configure(service, {}, '')['folder'], '')

    def test_failed_marker_write_preserves_saved_settings(self):
        service = self.make_service()
        self.configure(service)
        previous = self.store.get_backup()
        new = self.base / 'new'
        new.mkdir()
        real_open = os.open

        def open_file(path, *args, **kwargs):
            if path == MARKER:
                raise PermissionError('synthetic failure')
            return real_open(path, *args, **kwargs)

        with patch('app.backup.os.open', side_effect=open_file), self.assertRaises(BackupError) as caught:
            self.configure(service, folder=new)
        self.assertEqual(caught.exception.code, 'folder_readonly')
        self.assertEqual(self.store.get_backup(), previous)
        self.assertFalse((new / MARKER).exists())

    def test_rotation_keeps_writing_old_stream_and_finishes_only_complete_fragments(self):
        written = self.watch_pairs()
        service = self.make_service(segment_seconds=12)
        first, second = FakeStream(), FakeStream()
        self.sources.put(first)
        self.sources.put(second)
        self.configure(service)

        occupied = segment_path(self.root, 'cam1', NOW)
        occupied.parent.mkdir(parents=True)
        occupied.write_bytes(b'existing recording')
        abandoned = occupied.with_name('12-00-01.mp4.part')
        abandoned.write_bytes(b'existing partial')

        service.start()
        self.assertEqual(self.opened.get(timeout=1), 'cam1')
        first.feed(HEADER + PAIR[:11])
        first.feed(PAIR[11:])
        first_path = written.get(timeout=1)
        self.assertEqual(first_path.name, '12-00-02.mp4')
        self.elapsed = 12
        first.feed(PAIR)
        self.assertEqual(self.opened.get(timeout=1), 'cam1')
        second.feed(HEADER)
        first.feed(PAIR)
        self.assertEqual(written.get(timeout=1), first_path)
        self.assertEqual(written.get(timeout=1), first_path)
        self.assertFalse(first_path.exists())
        self.assertFalse(first.closed.is_set())

        second.feed(MOOF)
        self.assertTrue(first.closed.wait(1))
        second.feed(VIDEO + MOOF + VIDEO[:10])
        second_path = written.get(timeout=1)
        service.stop()
        self.assertTrue(second.closed.wait(1))
        self.assertEqual(second_path.name, '12-00-03.mp4')
        self.assertEqual(first_path.read_bytes(), HEADER + PAIR * 3)
        self.assertEqual(second_path.read_bytes(), HEADER + PAIR)
        self.assertEqual(occupied.read_bytes(), b'existing recording')
        self.assertEqual(abandoned.read_bytes(), b'existing partial')
        self.assertEqual(stat.S_IMODE(second_path.stat().st_mode), 0o600)
        self.assertEqual(service.status()['cameras'][0]['last'], NOW.isoformat())

    def test_stall_finalizes_without_waiting_on_the_stream(self):
        written, delays = self.watch_pairs(), queue.Queue()
        service = self.make_service(stall_seconds=20, wait=lambda delay: delays.put(delay) or True)
        stream = FakeStream()
        self.sources.put(stream)
        self.configure(service)
        service.start()
        stream.feed(HEADER + PAIR + MOOF)
        path = written.get(timeout=1)
        self.elapsed = 20
        worker = service._workers['cam1']
        with worker.ready:
            worker.ready.notify_all()
        self.assertEqual(delays.get(timeout=1), 5)
        self.assertTrue(stream.closed.wait(1))
        self.assertEqual(path.read_bytes(), HEADER + PAIR)
        self.assertFalse(path.with_suffix('.mp4.part').exists())

    def test_failed_write_finalizes_at_the_last_complete_mdat(self):
        delays = queue.Queue()
        service = self.make_service(wait=lambda delay: delays.put(delay) or True)
        stream = FakeStream()
        self.sources.put(stream)
        self.configure(service)
        original = backup._Segment._write

        def write(segment, data):
            if data == box(b'mdat', b'next'):
                original(segment, data[:9])
                raise OSError('synthetic partial write')
            original(segment, data)

        with patch.object(backup._Segment, '_write', write):
            service.start()
            stream.feed(HEADER + PAIR + MOOF + box(b'mdat', b'next'))
            self.assertEqual(delays.get(timeout=1), 5)
        service.stop()
        path = segment_path(self.root, 'cam1', NOW)
        self.assertEqual(path.read_bytes(), HEADER + PAIR)

    def test_finalization_uses_the_next_second_if_a_collision_appears_mid_recording(self):
        written = self.watch_pairs()
        service = self.make_service()
        stream = FakeStream()
        self.sources.put(stream)
        self.configure(service)
        service.start()
        stream.feed(HEADER + PAIR)
        path = written.get(timeout=1)
        path.write_bytes(b'keep this recording')
        next_partial = path.with_name('12-00-01.mp4.part')
        next_partial.write_bytes(b'keep this partial')
        service.stop()
        self.assertEqual(path.read_bytes(), b'keep this recording')
        self.assertEqual(next_partial.read_bytes(), b'keep this partial')
        self.assertEqual(path.with_name('12-00-02.mp4').read_bytes(), HEADER + PAIR)

    def test_eof_without_full_fragment_discards_partial_and_backs_off(self):
        delays = queue.Queue()
        retry = [5, 10, 20, 40, 60, 60, 60]
        finished = threading.Event()

        def wait(delay):
            delays.put(delay)
            if delays.qsize() >= len(retry):
                finished.set()
                return True
            return False

        service = self.make_service(wait=wait)
        stream = FakeStream()
        self.sources.put(stream)
        self.configure(service)
        service.start()
        stream.feed(HEADER + MOOF + VIDEO[:9])
        stream.feed(b'')
        self.assertTrue(finished.wait(1))
        service.stop()
        self.assertEqual([delays.get_nowait() for _ in retry], retry)
        self.assertEqual(list(self.root.glob('cam1/*/*')), [])

    def test_low_space_missing_folder_and_status_reads_use_cached_storage(self):
        delays = queue.Queue()
        release = threading.Event()

        def wait(delay):
            delays.put(delay)
            release.wait(1)
            return True

        service = self.make_service(wait=wait)
        self.addCleanup(release.set)
        self.configure(service)
        self.free_space.return_value = 0
        service.start()
        self.assertEqual(delays.get(timeout=1), 30)
        status = service.status()
        self.assertEqual(status['warning'], 'low_space')
        self.assertEqual(status['cameras'][0]['state'], 'low_space')
        self.assertEqual(status['free_bytes'], 0)
        self.assertTrue(self.opened.empty())
        release.set()
        service.stop()
        with patch('app.backup.os.scandir', side_effect=AssertionError('disk walk')), \
                patch('app.backup.shutil.disk_usage', side_effect=AssertionError('disk query')):
            self.assertEqual(service.status()['warning'], 'low_space')

        moved = self.base / 'unmounted'
        self.root.rename(moved)
        service.start()
        self.assertEqual(delays.get(timeout=1), 30)
        service.stop()
        self.assertEqual(service.status()['warning'], 'folder_missing')
        self.assertFalse(self.root.exists())
        self.assertTrue(self.opened.empty())

    def test_recording_pauses_and_retries_when_only_the_last_24_hours_remain(self):
        delays, resume = queue.Queue(), queue.Queue()

        def wait(delay):
            delays.put(delay)
            return resume.get(timeout=2)

        service = self.make_service(wait=wait)
        self.addCleanup(resume.put, True)
        self.configure(service)
        old, boundary, recent = [
            segment_path(self.root, 'cam1', NOW - datetime.timedelta(hours=hours))
            for hours in (48, 24, 1)
        ]
        for path in (old, boundary, recent):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(PAIR)
        self.free_space.return_value = MIN_FREE_BYTES - 1

        service.start()
        self.assertEqual(delays.get(timeout=1), 30)
        service._maintenance_pass()
        self.assertFalse(old.exists())
        self.assertEqual(boundary.read_bytes(), PAIR)
        self.assertEqual(recent.read_bytes(), PAIR)
        status = service.status()
        self.assertEqual(status['warning'], 'low_space')
        self.assertEqual(status['cameras'][0]['state'], 'low_space')
        self.assertEqual(status['used_bytes'], 2 * len(PAIR))

        resume.put(False)
        self.assertEqual(delays.get(timeout=1), 30)
        self.assertTrue(self.opened.empty())

    def test_removing_one_camera_finalizes_its_file_while_the_other_keeps_recording(self):
        written = self.watch_pairs()
        front, yard = FakeStream(), FakeStream()
        streams = {'cam1': front, 'cam2': yard}
        service = BackupService(self.store, streams.__getitem__, (self.project,),
                                now=lambda: NOW, free_space=self.free_space)
        self.addCleanup(service.stop)
        self.configure(service, {'cam1': 7, 'cam2': 7})
        service.start()
        front.feed(HEADER + PAIR)
        yard.feed(HEADER + PAIR)
        paths = {written.get(timeout=1), written.get(timeout=1)}
        self.store.remove('cam1')
        service.settings_changed()
        self.assertTrue(front.closed.wait(1))
        self.assertEqual([camera['id'] for camera in service.status()['cameras']], ['cam2'])
        yard.feed(PAIR)
        yard_path = written.get(timeout=1)
        self.assertFalse(yard.closed.is_set())
        self.assertEqual(yard_path, segment_path(self.root, 'cam2', NOW))
        service.stop()
        for path in paths:
            self.assertTrue(path.is_file())
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.parent.parent.stat().st_mode), 0o700)
        self.assertEqual(yard_path.read_bytes(), HEADER + PAIR * 2)

    def test_maintenance_uses_new_folder_and_only_prunes_enabled_cameras(self):
        service = self.make_service()
        self.configure(service)
        old_recording = segment_path(self.root, 'cam1', NOW - datetime.timedelta(days=15))
        old_recording.parent.mkdir(parents=True)
        old_recording.write_bytes(PAIR)
        new = self.base / 'new'
        new.mkdir()
        self.configure(service, folder=new)
        expired = segment_path(new, 'cam1', NOW - datetime.timedelta(days=8))
        retained = segment_path(new, 'cam1', NOW)
        disabled = segment_path(new, 'cam2', NOW - datetime.timedelta(days=60))
        for path in (expired, retained, disabled):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(PAIR)
        service._maintenance_pass()
        self.assertFalse(expired.exists())
        self.assertTrue(old_recording.exists())
        self.assertTrue(disabled.exists())
        self.assertTrue((new / 'README.txt').is_file())
        self.assertEqual(service.status()['used_bytes'], len(PAIR) * 2)

    def test_recording_starts_when_maintenance_recovers_space(self):
        written = self.watch_pairs()
        service = self.make_service()
        stream = FakeStream()
        self.sources.put(stream)
        self.configure(service)
        recording = segment_path(self.root, 'cam1', NOW - datetime.timedelta(days=2))
        recording.parent.mkdir(parents=True)
        recording.write_bytes(PAIR)
        self.free_space.side_effect = lambda: 0 if recording.exists() else PLENTY
        service.start()
        self.assertEqual(self.opened.get(timeout=1), 'cam1')
        self.assertFalse(recording.exists())
        stream.feed(HEADER + PAIR)
        path = written.get(timeout=1)
        service.stop()
        self.assertEqual(path.read_bytes(), HEADER + PAIR)
        self.assertIsNone(service.status()['warning'])

    def test_prune_rejects_a_symlinked_marker_or_root(self):
        old = segment_path(self.root, 'cam1', NOW - datetime.timedelta(days=3))
        old.parent.mkdir(parents=True)
        old.write_bytes(PAIR)
        marker = self.base / 'marker'
        marker.write_text('')
        (self.root / MARKER).symlink_to(marker)
        self.assertEqual(prune(self.root, {'cam1': 1}, NOW, lambda: 0, MIN_FREE_BYTES), [])
        other = self.base / 'other'
        other.mkdir()
        (other / MARKER).write_text('')
        recording = segment_path(other, 'cam1', NOW - datetime.timedelta(days=3))
        recording.parent.mkdir(parents=True)
        recording.write_bytes(PAIR)
        alias = self.base / 'alias'
        alias.symlink_to(other, target_is_directory=True)
        self.assertEqual(prune(str(alias) + '/', {'cam1': 1}, NOW, lambda: 0, MIN_FREE_BYTES), [])
        self.assertTrue(old.exists() and recording.exists())
