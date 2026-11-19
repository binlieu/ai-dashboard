from contextlib import contextmanager
import datetime
import errno
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


def companion(path):
    return path.with_name('._' + path.name)


@contextmanager
def fat_volume(link_error=errno.ENOTSUP):
    rename = Mock(return_value=-1)
    libc = Mock(renameatx_np=rename, renameat2=rename)
    with patch('app.backup.ctypes.CDLL', return_value=libc), \
            patch('app.backup.ctypes.get_errno', return_value=errno.ENOTSUP), \
            patch('app.backup.os.link', side_effect=OSError(link_error, 'synthetic link failure')) as link:
        yield rename, link


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

    def assert_awaits_worker(self, action, worker, blocked, release, final):
        joining, result = threading.Event(), queue.Queue()
        original_join = worker.thread.join

        def join(timeout):
            joining.set()
            original_join(timeout)

        def run():
            action()
            result.put(final.is_file())

        caller = threading.Thread(target=run, daemon=True)
        with patch.object(worker.thread, 'join', side_effect=join):
            caller.start()
            try:
                self.assertTrue(blocked.wait(1))
                self.assertTrue(joining.wait(1))
                self.assertTrue(caller.is_alive())
            finally:
                release.set()
                caller.join(1)
        self.assertTrue(result.get(timeout=1))
        self.assertFalse(worker.thread.is_alive())

    def assert_awaits_slow_rename(self, action, worker, final):
        renaming, release = threading.Event(), threading.Event()
        original_rename = os.rename

        def rename(*args, **kwargs):
            renaming.set()
            if not release.wait(2):
                raise OSError('Synthetic rename was not released')
            return original_rename(*args, **kwargs)

        with fat_volume(), patch('app.backup.os.rename', side_effect=rename):
            self.assert_awaits_worker(action, worker, renaming, release, final)

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

    def test_fat_volume_uses_plain_rename_and_removes_the_partial_companion(self):
        final = self.root / '12-00-00.mp4'
        partial = final.with_suffix('.mp4.part')
        partial.write_bytes(HEADER + PAIR)
        companion(partial).write_bytes(b'synthetic metadata')
        with backup._directory(self.root) as date_fd, fat_volume() as (exclusive, link), \
                patch('app.backup.os.rename', wraps=os.rename) as rename:
            self.assertTrue(backup._publish(partial.name, final.name, date_fd, date_fd))
            exclusive.assert_called_once()
            link.assert_called_once()
            rename.assert_called_once_with(partial.name, final.name, src_dir_fd=date_fd, dst_dir_fd=date_fd)
        self.assertEqual(final.read_bytes(), HEADER + PAIR)
        self.assertFalse(partial.exists())
        self.assertFalse(companion(partial).exists())

    def test_fat_volume_collision_leaves_existing_recording_and_complete_partial(self):
        written = self.watch_pairs()
        service = self.make_service()
        stream = FakeStream()
        self.sources.put(stream)
        self.configure(service)
        with fat_volume(), patch('app.backup.os.rename', wraps=os.rename) as rename:
            service.start()
            stream.feed(HEADER + PAIR)
            final = written.get(timeout=1)
            final.write_bytes(b'existing recording')
            service.stop()
            rename.assert_not_called()
        partial = final.with_suffix('.mp4.part')
        self.assertEqual(final.read_bytes(), b'existing recording')
        self.assertEqual(partial.read_bytes(), HEADER + PAIR)
        self.assertEqual(set(final.parent.iterdir()), {final, partial})
        self.assertIsNone(service.status()['cameras'][0]['last'])

    def test_fat_volume_treats_a_dangling_symlink_as_an_existing_final_name(self):
        final = self.root / '12-00-00.mp4'
        final.symlink_to(self.root / 'missing')
        partial = final.with_suffix('.mp4.part')
        partial.write_bytes(HEADER + PAIR)
        with backup._directory(self.root) as date_fd, fat_volume(), \
                patch('app.backup.os.rename', wraps=os.rename) as rename:
            self.assertFalse(backup._publish(partial.name, final.name, date_fd, date_fd))
            rename.assert_not_called()
        self.assertTrue(final.is_symlink())
        self.assertEqual(partial.read_bytes(), HEADER + PAIR)

    def test_publish_does_not_fall_back_to_plain_rename_on_link_io_error(self):
        final = self.root / '12-00-00.mp4'
        partial = final.with_suffix('.mp4.part')
        partial.write_bytes(HEADER + PAIR)
        with backup._directory(self.root) as date_fd, fat_volume(errno.EIO), \
                patch('app.backup.os.rename', wraps=os.rename) as rename:
            with self.assertRaises(OSError) as caught:
                backup._publish(partial.name, final.name, date_fd, date_fd)
            self.assertEqual(caught.exception.errno, errno.EIO)
            rename.assert_not_called()
        self.assertEqual(partial.read_bytes(), HEADER + PAIR)
        self.assertFalse(final.exists())

    def test_failed_finalize_leaves_partial_and_next_segment_records(self):
        written = self.watch_pairs()
        service = self.make_service(segment_seconds=12)
        first, second = FakeStream(), FakeStream()
        self.sources.put(first)
        self.sources.put(second)
        self.configure(service)
        original = backup._publish
        first_name = segment_path(self.root, 'cam1', NOW).with_suffix('.mp4.part').name

        def publish(partial, final, source_fd, destination_fd):
            if partial == first_name:
                raise OSError(errno.EIO, 'synthetic finalization failure')
            return original(partial, final, source_fd, destination_fd)

        with patch.object(backup, '_publish', side_effect=publish):
            service.start()
            self.assertEqual(self.opened.get(timeout=1), 'cam1')
            first.feed(HEADER + PAIR)
            first_path = written.get(timeout=1)
            self.elapsed = 12
            first.feed(b'\x00')
            self.assertEqual(self.opened.get(timeout=1), 'cam1')
            second.feed(HEADER + PAIR)
            next_path = written.get(timeout=1)
            self.assertNotEqual(next_path, first_path)
            self.assertIsNone(service.status()['cameras'][0]['last'])
            service.stop()
        partial = first_path.with_suffix('.mp4.part')
        self.assertFalse(first_path.exists())
        self.assertEqual(partial.read_bytes(), HEADER + PAIR)
        self.assertEqual(next_path.read_bytes(), HEADER + PAIR)
        self.assertEqual(service.status()['cameras'][0]['last'], NOW.isoformat())
        removed = prune(self.root, {'cam1': 7}, NOW + datetime.timedelta(hours=2), lambda: PLENTY, MIN_FREE_BYTES)
        self.assertEqual(removed, [partial])
        self.assertTrue(next_path.exists())

    def test_stop_waits_for_slow_finalization(self):
        written = self.watch_pairs()
        service = self.make_service()
        stream = FakeStream()
        self.sources.put(stream)
        self.configure(service)
        service.start()
        stream.feed(HEADER + PAIR)
        final = written.get(timeout=1)

        self.assert_awaits_slow_rename(service.stop, service._workers['cam1'], final)
        self.assertEqual(final.read_bytes(), HEADER + PAIR)
        self.assertFalse(final.with_suffix('.mp4.part').exists())

    def test_stop_joins_a_worker_that_has_released_its_camera_slot(self):
        written = self.watch_pairs()
        service = self.make_service(wait=lambda delay: True)
        stream = FakeStream()
        self.sources.put(stream)
        self.configure(service)
        finishing, release, maintained = threading.Event(), threading.Event(), threading.Event()
        original_work = service._work

        def work(worker):
            original_work(worker)
            finishing.set()
            release.wait(2)

        self.addCleanup(release.set)
        with patch.object(service, '_work', side_effect=work), \
                patch.object(service, '_maintenance_pass', side_effect=maintained.set):
            service.start()
            self.assertTrue(maintained.wait(1))
            worker = service._workers['cam1']
            stream.feed(HEADER + PAIR)
            final = written.get(timeout=1)
            stream.feed(b'')
            self.assertTrue(finishing.wait(1))
            self.assertNotIn('cam1', service._workers)
            self.assert_awaits_worker(service.stop, worker, finishing, release, final)

    def test_configure_waits_for_old_file_on_folder_change_and_turning_off(self):
        written = self.watch_pairs()
        service = self.make_service()
        first, second = FakeStream(), FakeStream()
        self.sources.put(first)
        self.sources.put(second)
        self.configure(service)
        new = self.base / 'new'
        new.mkdir()
        service.start()
        first.feed(HEADER + PAIR)
        first_final = written.get(timeout=1)

        self.assert_awaits_slow_rename(lambda: self.configure(service, folder=new),
                                      service._workers['cam1'], first_final)
        second.feed(HEADER + PAIR)
        second_final = written.get(timeout=1)
        self.assertEqual(second_final, segment_path(new, 'cam1', NOW))
        self.assert_awaits_slow_rename(lambda: self.configure(service, {'cam1': 0}, folder=new),
                                      service._workers['cam1'], second_final)
        self.assertEqual(first_final.read_bytes(), HEADER + PAIR)
        self.assertEqual(second_final.read_bytes(), HEADER + PAIR)
        self.assertEqual(service.status()['cameras'][0]['state'], 'off')

    def test_two_unsaved_segments_warn_until_a_segment_saves(self):
        written = self.watch_pairs()
        service = self.make_service(segment_seconds=12)
        streams = [FakeStream() for _ in range(4)]
        for stream in streams:
            self.sources.put(stream)
        self.configure(service)
        publish = backup._publish
        outcomes = iter((False, RuntimeError('synthetic finalization failure'), True, True))

        def publish_with_failures(*args):
            saved = next(outcomes)
            if isinstance(saved, Exception):
                raise saved
            return publish(*args) if saved else False

        with patch.object(backup, '_publish', side_effect=publish_with_failures):
            service.start()
            self.assertEqual(self.opened.get(timeout=1), 'cam1')
            streams[0].feed(HEADER + PAIR)
            paths = [written.get(timeout=1)]
            for previous, current, warning in zip(streams, streams[1:], (None, 'save_failed', None)):
                self.elapsed += 12
                previous.feed(b'\x00')
                self.assertEqual(self.opened.get(timeout=1), 'cam1')
                current.feed(HEADER + PAIR)
                paths.append(written.get(timeout=1))
                status = service.status()
                self.assertEqual(status['cameras'][0]['state'], warning or 'recording')
                self.assertEqual(status['cameras'][1]['state'], 'off')
                self.assertEqual(status['warning'], warning)
                if warning:
                    self.assertIsNone(status['cameras'][0]['last'])
                    # Exercise the status priorities before allowing the worker to write again.
                    with service._lock:
                        for storage_warning in ('low_space', 'folder_missing', None):
                            service._storage_status(str(self.root), PLENTY, None, storage_warning)
                            self.assertEqual(service.status()['warning'], storage_warning or 'save_failed')
            self.assertEqual(service.status()['cameras'][0]['last'], NOW.isoformat())
            service.stop()
        for path in paths[:2]:
            self.assertFalse(path.exists())
            self.assertEqual(path.with_suffix('.mp4.part').read_bytes(), HEADER + PAIR)
        for path in paths[2:]:
            self.assertEqual(path.read_bytes(), HEADER + PAIR)

    def test_failed_first_write_cleans_up_empty_partial_and_its_companion(self):
        delays = queue.Queue()
        service = self.make_service(wait=lambda delay: delays.put(delay) or True)
        stream = FakeStream()
        self.sources.put(stream)
        self.configure(service)

        def fail_write(segment, data):
            companion(segment.partial).write_bytes(b'synthetic metadata')
            file = segment._file

            def fail_close():
                file.close()
                raise OSError(errno.EIO, 'synthetic close failure')

            segment._file = Mock(wraps=file)
            segment._file.close.side_effect = fail_close
            raise OSError(errno.ENODEV, 'synthetic write failure')

        with patch.object(backup._Segment, '_write', fail_write):
            service.start()
            stream.feed(HEADER + PAIR)
            self.assertEqual(delays.get(timeout=1), 5)
        service.stop()
        final = segment_path(self.root, 'cam1', NOW)
        self.assertFalse(final.exists())
        self.assertFalse(final.with_suffix('.mp4.part').exists())
        self.assertFalse(companion(final.with_suffix('.mp4.part')).exists())

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
            companion(path).write_bytes(b'synthetic metadata')
        service._maintenance_pass()
        self.assertFalse(expired.exists())
        self.assertFalse(companion(expired).exists())
        self.assertTrue(old_recording.exists())
        self.assertTrue(disabled.exists())
        self.assertTrue(companion(disabled).exists())
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

    def test_prune_removes_only_companions_of_deleted_recordings_and_orphan_only_dates(self):
        (self.root / MARKER).touch()
        expired = segment_path(self.root, 'cam1', NOW - datetime.timedelta(days=8))
        stale = segment_path(self.root, 'cam1', NOW - datetime.timedelta(hours=2)).with_suffix('.mp4.part')
        retained = segment_path(self.root, 'cam1', NOW)
        orphan = segment_path(self.root, 'cam1', NOW - datetime.timedelta(days=2))
        symlinked = segment_path(self.root, 'cam1', NOW - datetime.timedelta(days=9))
        unrelated = orphan.parent.with_name('2026-11-12') / '._notes.txt'
        ds_store = orphan.parent.with_name('2026-11-11') / '.DS_Store'
        sentinel = self.base / 'target'
        for recording in (expired, stale, retained, symlinked):
            recording.parent.mkdir(parents=True, exist_ok=True)
            recording.touch()
        sidecars = [companion(path) for path in (expired, stale, retained, orphan, orphan.with_suffix('.mp4.part'))]
        for path in sidecars + [unrelated, ds_store, sentinel]:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'synthetic metadata')
        companion(symlinked).symlink_to(sentinel)

        removed = prune(self.root, {'cam1': 7}, NOW, lambda: PLENTY, MIN_FREE_BYTES)
        self.assertEqual(removed, [symlinked, expired, stale])
        self.assertFalse(expired.parent.exists())
        self.assertFalse(orphan.parent.exists())
        self.assertFalse(companion(stale).exists())
        self.assertTrue(companion(retained).exists())
        self.assertTrue(companion(symlinked).is_symlink())
        for path in (unrelated, ds_store, sentinel):
            self.assertEqual(path.read_bytes(), b'synthetic metadata')


class PlaceholderIdentityTests(unittest.TestCase):
    """exFAT gives a new empty file a placeholder inode that changes once data is written."""

    def test_segment_is_finalised_when_the_creation_identity_was_a_placeholder(self):
        import datetime
        import os
        import struct
        import tempfile
        from pathlib import Path
        from app import backup

        def box(kind, size):
            return struct.pack(">I4s", size, kind) + bytes(size - 8)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            other = root / "other"
            other.write_bytes(b"x")
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                segment = backup._Segment(root, "cam4", datetime.datetime(2026, 1, 1), root_fd,
                                          bytearray(box(b"ftyp", 24) + box(b"moov", 40)))
                segment._identity = os.stat(other)
                segment.box(box(b"moof", 16))
                segment.box(box(b"mdat", 32))
                self.assertTrue(segment.finish())
            finally:
                os.close(root_fd)
            self.assertTrue((root / "cam4/2026-01-01/00-00-00.mp4").is_file())
            self.assertFalse((root / "cam4/2026-01-01/00-00-00.mp4.part").exists())

    def test_segment_is_finalised_when_the_open_file_reports_a_placeholder_inode(self):
        import datetime
        import os
        import struct
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from app import backup

        def box(kind, size):
            return struct.pack(">I4s", size, kind) + bytes(size - 8)

        real_fstat = os.fstat

        def placeholder_fstat(fd):
            import types
            info = real_fstat(fd)
            fields = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
            fields["st_ino"] = 2 ** 64 - 33
            return types.SimpleNamespace(**fields)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with patch.object(backup.os, "fstat", placeholder_fstat):
                    segment = backup._Segment(root, "cam4", datetime.datetime(2026, 1, 1), root_fd,
                                              bytearray(box(b"ftyp", 24) + box(b"moov", 40)))
                    segment.box(box(b"moof", 16))
                    segment.box(box(b"mdat", 32))
                    self.assertTrue(segment.finish())
            finally:
                os.close(root_fd)
            self.assertTrue((root / "cam4/2026-01-01/00-00-00.mp4").is_file())

    def test_a_replaced_partial_is_not_published(self):
        import datetime
        import os
        import struct
        import tempfile
        from pathlib import Path
        from app import backup

        def box(kind, size):
            return struct.pack(">I4s", size, kind) + bytes(size - 8)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                segment = backup._Segment(root, "cam4", datetime.datetime(2026, 1, 1), root_fd,
                                          bytearray(box(b"ftyp", 24) + box(b"moov", 40)))
                segment.box(box(b"moof", 16))
                segment.box(box(b"mdat", 32))
                partial = root / "cam4/2026-01-01/00-00-00.mp4.part"
                partial.unlink()
                partial.write_bytes(b"someone else's file, different size")
                self.assertFalse(segment.finish())
            finally:
                os.close(root_fd)
            self.assertFalse((root / "cam4/2026-01-01/00-00-00.mp4").exists())
            self.assertEqual(partial.read_bytes(), b"someone else's file, different size")
