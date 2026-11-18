import concurrent.futures
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.store import CameraStore, StoreError, seed_state, validate_name

MAC = 'AA:BB:CC:DD:EE:01'


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'data' / 'cameras.json'

    def test_seed_and_default_names(self):
        store = CameraStore(self.path, {'CAMERA_MACS': MAC+', AA-BB-CC-DD-EE-02', 'CAMERA_NAMES': ' Front , '})
        state = store.snapshot()
        self.assertEqual(state['next_id'], 3)
        self.assertEqual([camera['name'] for camera in state['cameras']], ['Front', 'Camera 2'])
        self.assertEqual(state['cameras'][0]['mac'], MAC.lower())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)

    def test_legacy_seed_and_empty_setup(self):
        self.assertEqual(seed_state({'CAMERA_MAC': MAC})['cameras'][0]['id'], 'cam1')
        self.assertEqual(seed_state({'CAMERA_MACS': '', 'CAMERA_MAC': MAC})['cameras'], [])
        self.assertEqual(seed_state({})['cameras'], [])

    def test_seed_is_never_read_again(self):
        CameraStore(self.path).add(MAC)
        seed = Mock(side_effect=AssertionError('seed must not be read'))
        self.assertEqual(len(CameraStore(self.path, seed).snapshot()['cameras']), 1)
        seed.assert_not_called()

    def test_seed_validation(self):
        for values in ({'CAMERA_MACS': 'invalid'}, {'CAMERA_MACS': MAC+','+MAC.lower()},
                       {'CAMERA_MACS': MAC+','}, {'CAMERA_MAC': MAC+','+MAC},
                       {'CAMERA_MACS': MAC, 'CAMERA_NAMES': 'x'*41}):
            with self.subTest(values=values), self.assertRaises(StoreError):
                seed_state(values)

    def test_ids_never_reused_after_delete_or_rollback(self):
        store = CameraStore(self.path)
        before = store.snapshot()
        store.add(MAC)
        store.restore(before)
        self.assertEqual(store.add(MAC)['id'], 'cam2')
        store.remove('cam2')
        self.assertEqual(store.add(MAC)['id'], 'cam3')
        self.assertEqual(CameraStore(self.path).snapshot()['next_id'], 4)

    def test_duplicate_limit_and_id_exhaustion(self):
        store = CameraStore(self.path)
        store.add(MAC)
        with self.assertRaises(StoreError) as error:
            store.add(MAC.replace(':', '-'))
        self.assertEqual(error.exception.code, 'duplicate')
        for index in range(15):
            store.add('00:11:22:33:44:%02x' % index)
        with self.assertRaises(StoreError) as error:
            store.add('00:11:22:33:44:ff')
        self.assertEqual(error.exception.code, 'limit')
        empty = CameraStore(self.path.with_name('exhausted.json'))
        state = empty.snapshot()
        state['next_id'] = 10000
        empty.restore(state)
        with self.assertRaises(StoreError) as error:
            empty.add(MAC)
        self.assertEqual(error.exception.code, 'limit')

    def test_names_and_renames(self):
        self.assertEqual(validate_name(' Cửa trước '), 'Cửa trước')
        for name in ('', ' ', 'x'*41, 'name\n', '\tname', 'name\x00', '\x7f', None, 2):
            with self.subTest(name=name), self.assertRaises(StoreError):
                validate_name(name)
        store = CameraStore(self.path)
        camera = store.add(MAC)
        self.assertEqual(store.rename(camera['id'], ' New ')['name'], 'New')
        with self.assertRaises(StoreError):
            store.rename('cam999', 'New')

    def test_layouts(self):
        store = CameraStore(self.path)
        for layout in ('auto', '2x1', '2x2', '3x3', '4x4', 'spotlight'):
            store.set_layout(layout)
            self.assertEqual(CameraStore(self.path).snapshot()['layout'], layout)
        with self.assertRaises(StoreError):
            store.set_layout('exec:bad')

    def test_atomic_replace_failure_preserves_file_and_memory(self):
        store = CameraStore(self.path)
        before = self.path.read_bytes()
        with patch('app.store.os.replace', side_effect=OSError('synthetic write failure')):
            with self.assertRaises(OSError):
                store.add(MAC)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(store.snapshot()['cameras'], [])
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_corruption_backed_up_without_reseeding(self):
        self.path.parent.mkdir()
        self.path.write_text('not JSON', encoding='utf-8')
        warning, seed = Mock(), Mock(side_effect=AssertionError('no reseed on corruption'))
        store = CameraStore(self.path, seed, warning)
        backups = list(self.path.parent.glob('cameras.json.corrupt-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), 'not JSON')
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
        self.assertEqual(store.snapshot()['cameras'], [])
        warning.assert_called_once()
        seed.assert_not_called()

    def test_bad_schema_recovers(self):
        CameraStore(self.path)
        state = json.loads(self.path.read_text())
        state['next_id'] = 0
        self.path.write_text(json.dumps(state))
        self.assertEqual(CameraStore(self.path, warning=lambda _: None).snapshot()['next_id'], 1)

    def test_concurrent_mutations_have_unique_ids(self):
        store = CameraStore(self.path)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            cameras = list(executor.map(lambda index: store.add('00:11:22:33:44:%02x' % index), range(16)))
        self.assertEqual(len({camera['id'] for camera in cameras}), 16)
        self.assertEqual(len(CameraStore(self.path).snapshot()['cameras']), 16)
