import concurrent.futures
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.store import CameraStore, StoreError, WEATHER_DEFAULTS, seed_state, validate_name

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

    def test_legacy_weather_defaults_do_not_change_existing_file_shape(self):
        store = CameraStore(self.path, {'CAMERA_MAC': MAC})
        original = self.path.read_bytes()
        self.assertNotIn('weather', store.snapshot())
        self.assertEqual(store.get_weather(), WEATHER_DEFAULTS)
        self.assertEqual(CameraStore(self.path).get_weather(), WEATHER_DEFAULTS)
        self.assertEqual(self.path.read_bytes(), original)
        settings = store.get_weather()
        settings['enabled'] = True
        self.assertEqual(store.get_weather(), WEATHER_DEFAULTS)

    def test_weather_preserved_across_camera_and_layout_changes_and_rollback(self):
        store = CameraStore(self.path)
        previous = store.snapshot()
        weather = dict(WEATHER_DEFAULTS, enabled=True, place='Springfield',
                       name='Springfield, Illinois, United States', lat=39.8, lon=-89.64)
        store.set_weather(weather)
        camera = store.add(MAC)
        store.rename(camera['id'], 'Camera')
        store.set_layout('spotlight')
        store.remove(camera['id'])
        self.assertEqual(CameraStore(self.path).get_weather(), weather)
        store.restore(previous)
        self.assertEqual(CameraStore(self.path).get_weather(), weather)
        weather['place'] = '90210'
        self.assertEqual(store.get_weather()['place'], 'Springfield')

    def test_invalid_weather_resets_only_weather_and_warns_once(self):
        store = CameraStore(self.path, {'CAMERA_MAC': MAC})
        original = store.snapshot()
        bad = [None, [], {}, dict(WEATHER_DEFAULTS, enabled=1), dict(WEATHER_DEFAULTS, units='k'),
               dict(WEATHER_DEFAULTS, lat=0), dict(WEATHER_DEFAULTS, lat=91, lon=0),
               dict(WEATHER_DEFAULTS, lat=0, lon=-181), dict(WEATHER_DEFAULTS, lat=True, lon=0),
               dict(WEATHER_DEFAULTS, lat=float('nan'), lon=0), dict(WEATHER_DEFAULTS, enabled=True),
               dict(WEATHER_DEFAULTS, place='x' * 81), dict(WEATHER_DEFAULTS, name='x' * 121),
               dict(WEATHER_DEFAULTS, place='Springfield\n'), dict(WEATHER_DEFAULTS, extra=1)]
        for weather in bad:
            with self.subTest(weather=weather):
                self.path.write_text(json.dumps(dict(original, weather=weather)))
                warning = Mock()
                loaded = CameraStore(self.path, warning=warning)
                self.assertEqual(loaded.snapshot()['cameras'], original['cameras'])
                self.assertEqual(loaded.get_weather(), WEATHER_DEFAULTS)
                self.assertEqual(json.loads(self.path.read_text())['weather'], WEATHER_DEFAULTS)
                self.assertEqual(list(self.path.parent.glob('*.corrupt-*')), [])
                warning.assert_called_once()
                with self.assertRaises((ValueError, TypeError)):
                    loaded.set_weather(weather)

    def test_weather_atomic_failure_does_not_change_settings(self):
        store = CameraStore(self.path)
        original = self.path.read_bytes()
        with patch('app.store.os.replace', side_effect=OSError('synthetic write failure')):
            with self.assertRaises(OSError):
                store.set_weather(dict(WEATHER_DEFAULTS, place='Springfield'))
        self.assertEqual(store.get_weather(), WEATHER_DEFAULTS)
        self.assertEqual(self.path.read_bytes(), original)

    def test_device_mutations_are_atomic_and_do_not_persist_runtime_fields(self):
        store = CameraStore(self.path, {'CAMERA_MAC': MAC})
        device = store.add_device('aa:bb:cc:dd:ee:02', 'Porch')
        before, disk = store.snapshot(), self.path.read_bytes()
        for action in (lambda: store.add_device('aa:bb:cc:dd:ee:03', 'Fan'),
                       lambda: store.rename_device(device['id'], 'New'),
                       lambda: store.remove_device(device['id'])):
            with patch('app.store.os.replace', side_effect=OSError('synthetic write failure')):
                with self.assertRaises(OSError):
                    action()
            self.assertEqual(store.snapshot(), before)
            self.assertEqual(self.path.read_bytes(), disk)
        copy = store.get_devices()
        copy[0]['name'] = 'Changed outside the store'
        self.assertEqual(store.get_devices()[0]['name'], 'Porch')
        self.assertEqual(set(json.loads(disk)['devices'][0]), {'id', 'mac', 'name'})

    def test_device_removal_and_counter_survive_camera_rollback(self):
        store = CameraStore(self.path)
        store.add_device('aa:bb:cc:dd:ee:02', 'Porch')
        previous = store.snapshot()
        store.add(MAC)
        store.remove_device('dev1')
        store.add_device('aa:bb:cc:dd:ee:03', 'Fan')
        store.restore(previous)
        self.assertEqual(store.snapshot()['cameras'], [])
        self.assertEqual(store.get_devices(), [{'id': 'dev2', 'mac': 'aa:bb:cc:dd:ee:03', 'name': 'Fan'}])
        self.assertEqual(CameraStore(self.path).add_device('aa:bb:cc:dd:ee:04', 'Light')['id'], 'dev3')

    def test_device_load_validation_isolated_from_cameras_and_weather(self):
        store = CameraStore(self.path, {'CAMERA_MAC': MAC})
        original = store.snapshot()
        weather = dict(WEATHER_DEFAULTS, place='Springfield')
        device = {'id': 'dev1', 'mac': 'aa:bb:cc:dd:ee:02', 'name': 'Porch'}
        invalid = [
            {'next_device_id': 2},
            {'devices': [], 'next_device_id': True},
            {'devices': [device], 'next_device_id': 2.0},
            {'devices': [dict(device, name=' ')], 'next_device_id': 2},
            {'devices': [dict(device, mac=device['mac'].upper())], 'next_device_id': 2},
            {'devices': [device, dict(device, mac='aa:bb:cc:dd:ee:03')], 'next_device_id': 2},
        ]
        for extra in invalid:
            with self.subTest(extra=extra):
                self.path.write_text(json.dumps(dict(original, weather=weather, **extra)))
                warning = Mock()
                loaded = CameraStore(self.path, warning=warning)
                self.assertEqual(loaded.snapshot()['cameras'], original['cameras'])
                self.assertEqual(loaded.get_weather(), weather)
                self.assertEqual(loaded.get_devices(), [])
                self.assertIs(type(json.loads(self.path.read_text())['next_device_id']), int)
                self.assertEqual(CameraStore(self.path).get_devices(), [])
                warning.assert_called_once()
                self.assertEqual(list(self.path.parent.glob('*.corrupt-*')), [])

    def test_concurrent_camera_and_device_adds_cannot_share_a_mac(self):
        store = CameraStore(self.path)
        def add(device):
            try:
                return store.add_device(MAC, 'Light') if device else store.add(MAC, 'Camera')
            except StoreError as error:
                return error.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(add, (True, False)))
        self.assertEqual(sum(result == 'duplicate' for result in results), 1)
        self.assertEqual(len(store.snapshot()['cameras']) + len(store.get_devices()), 1)

    def test_device_id_exhaustion_and_restore_mac_conflicts_preserve_data(self):
        store = CameraStore(self.path)
        camera = store.add(MAC)
        previous = store.snapshot()
        store.remove(camera['id'])
        device = store.add_device(MAC, 'Light')
        with self.assertRaises(StoreError) as error:
            store.restore(previous)
        self.assertEqual(error.exception.code, 'duplicate')
        self.assertEqual(CameraStore(self.path).get_devices(), [device])
        state = store.snapshot()
        state['next_device_id'] = 10001
        self.path.write_text(json.dumps(state))
        loaded = CameraStore(self.path)
        self.assertEqual(loaded.get_devices(), [device])
        with self.assertRaises(StoreError) as error:
            loaded.add_device('aa:bb:cc:dd:ee:02', 'Next')
        self.assertEqual((error.exception.code, error.exception.status), ('limit', 409))
