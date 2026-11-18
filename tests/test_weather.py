"""Weather cache and transport checks; all inputs, clocks and fetches are local fakes."""

import copy
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from app.store import CameraStore
from app.weather import (MAX_RESPONSE, WeatherError, WeatherService, _NoRedirect,
                         build_forecast_url, fetch_json, parse_forecast, parse_timezone, validate_settings)

PLACE = {"results": [{"name": "Springfield", "admin1": "Illinois", "country": "United States",
                       "latitude": 39.80172, "longitude": -89.64371}]}
SETTINGS = {"enabled": True, "place": "Springfield", "units": "f"}


def forecast_payload():
    return {"current": {"temperature_2m": 72, "apparent_temperature": 70, "weather_code": 0,
                        "wind_speed_10m": 8, "is_day": 1},
            "daily": {"time": ["2026-11-%s" % day for day in range(18, 23)],
                      "weather_code": [0, 2, 3, 63, 71], "temperature_2m_max": [75] * 5,
                      "temperature_2m_min": [50] * 5, "precipitation_probability_max": [0, 10, 20, 60, None]}}


class TransportTests(unittest.TestCase):
    def test_bounded_https_request_and_fixed_user_agent(self):
        opener = Mock(return_value=io.BytesIO(b'{"ok":true}'))
        url = build_forecast_url(1, 2, 'f')
        self.assertEqual(fetch_json(url, opener), {'ok': True})
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, url)
        self.assertEqual(request.header_items(), [('User-agent', 'kasa-camera-dashboard')])
        self.assertEqual(opener.call_args.kwargs, {'timeout': 10})

    def test_rejects_urls_before_opening(self):
        for url in ('http://api.open-meteo.com/v1/forecast', 'https://evil.example/',
                    'https://api.open-meteo.com.evil.example/', 'https://user@api.open-meteo.com/',
                    'https://api.open-meteo.com:444/', 'file:///tmp/weather'):
            opener = Mock()
            with self.subTest(url=url), self.assertRaises(WeatherError) as error:
                fetch_json(url, opener)
            self.assertEqual(error.exception.code, 'weather_unavailable')
            opener.assert_not_called()

    def test_failures_and_oversize_responses_are_safe(self):
        class Response(io.BytesIO):
            def read(self, size):
                self.requested = size
                return super().read(size)
        for raw in (b'not JSON', b'\xff', b' ' * MAX_RESPONSE, b' ' * (MAX_RESPONSE + 1)):
            response = Response(raw)
            with self.assertRaises(WeatherError) as error:
                fetch_json('https://api.open-meteo.com/v1/forecast', Mock(return_value=response))
            self.assertEqual(response.requested, MAX_RESPONSE)
            self.assertNotIn('not JSON', str(error.exception))
        with self.assertRaises(WeatherError):
            fetch_json('https://api.open-meteo.com/v1/forecast', Mock(side_effect=OSError('secret upstream text')))

    def test_default_opener_disables_proxies_and_refuses_redirects(self):
        with patch('app.weather.build_opener') as factory:
            factory.return_value.open.return_value = io.BytesIO(b'{}')
            self.assertEqual(fetch_json('https://api.open-meteo.com/v1/forecast'), {})
            proxy, redirect = factory.call_args.args
            self.assertEqual(proxy.proxies, {})
            self.assertIsInstance(redirect, _NoRedirect)
            for url in ('https://evil.example/', 'https://api.open-meteo.com/v1/forecast'):
                with self.assertRaises(WeatherError):
                    redirect.redirect_request(None, None, 302, '', {}, url)

    def test_invalid_calendar_dates_and_fixed_messages(self):
        payload = forecast_payload()
        payload['daily']['time'][0] = '2026-02-30'
        with self.assertRaises(WeatherError):
            parse_forecast(payload)
        self.assertNotIn('SENTINEL', str(WeatherError('weather_unavailable', 'SENTINEL')))
        for place in ('Springfield\x7f', 'Springfield\u202e'):
            with self.assertRaises(WeatherError):
                validate_settings(dict(SETTINGS, place=place))


class TimezoneTests(unittest.TestCase):
    def test_length_boundaries_and_ascii_segments(self):
        for name in ('A', 'A' * 64, 'Etc/GMT-5', 'A_1/B+2/C-3'):
            with self.subTest(name=name):
                self.assertEqual(parse_timezone({'timezone': name}), name)
        for name in ('A' * 65, 'A//B', 'A.B', 'A\\B', 'A\x00', 'Ａ/B', 'A/١'):
            with self.subTest(name=name):
                self.assertIsNone(parse_timezone({'timezone': name}))


class WeatherServiceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = CameraStore(Path(directory.name) / 'data/cameras.json')
        self.now = 100000
        self.fetch = Mock(side_effect=lambda url: copy.deepcopy(PLACE if '/search?' in url else forecast_payload()))
        self.service = WeatherService(self.store, self.fetch, lambda: self.now)
        self.addCleanup(self.service.stop)

    def enable(self):
        result = self.service.configure(SETTINGS)
        self.assertEqual(result['status'], 'loading')
        self.assertEqual(self.store.get_weather()['lat'], 39.8)
        return result

    def test_default_snapshot_never_fetches_and_does_not_expose_coordinates(self):
        self.assertEqual(self.service.snapshot(), {'enabled': False, 'place': '', 'units': 'f',
                                                   'name': '', 'forecast': None, 'status': 'off', 'timezone': None})
        self.service.refresh_once()
        self.fetch.assert_not_called()
        self.assertNotIn('lat', self.enable())
        self.assertNotIn('lon', self.service.snapshot())
        self.assertEqual(self.fetch.call_count, 1)

    def test_geocodes_only_when_enabled_and_location_needs_resolution(self):
        self.service.configure(dict(SETTINGS, enabled=False))
        self.fetch.assert_not_called()
        self.assertIsNone(self.store.get_weather()['lat'])
        self.enable()
        self.service.configure(dict(SETTINGS, units='c'))
        self.service.configure(dict(SETTINGS, enabled=False, units='c'))
        self.assertEqual(self.fetch.call_count, 1)
        self.service.configure(dict(SETTINGS, enabled=False, place='90210'))
        self.assertEqual(self.store.get_weather()['name'], '')
        self.assertIsNone(self.store.get_weather()['lat'])
        self.service.configure(dict(SETTINGS, place='90210'))
        self.assertEqual(self.fetch.call_count, 2)
        self.assertIn('countryCode=US', self.fetch.call_args.args[0])

    def test_same_place_without_coordinates_is_geocoded(self):
        self.service.configure(dict(SETTINGS, enabled=False))
        self.enable()
        self.assertEqual(self.fetch.call_count, 1)

    def test_cache_refresh_interval_failure_retry_and_expiry(self):
        self.enable()
        self.service.refresh_once()
        good = self.service.snapshot()['forecast']
        self.assertEqual(good['updated'], self.now)
        self.assertEqual(self.service.snapshot()['status'], 'ok')
        self.now += 899
        self.service.refresh_once()
        self.assertEqual(self.fetch.call_count, 2)
        self.now += 1
        self.fetch.side_effect = WeatherError('weather_unavailable')
        self.service.refresh_once()
        self.assertEqual(self.service.snapshot()['forecast'], good)
        self.now += 119
        self.service.refresh_once()
        self.assertEqual(self.fetch.call_count, 3)
        self.now += 1
        self.service.refresh_once()
        self.assertEqual(self.fetch.call_count, 4)
        self.now = good['updated'] + 3 * 3600
        self.assertEqual(self.service.snapshot()['status'], 'ok')
        self.now += 1
        self.assertEqual(self.service.snapshot()['status'], 'unavailable')
        self.assertIsNone(self.service.snapshot()['forecast'])
        self.fetch.side_effect = lambda url: forecast_payload()
        self.service.refresh_once()
        self.assertEqual(self.service.snapshot()['status'], 'ok')

    def test_initial_failure_and_configure_error_preserve_settings(self):
        self.enable()
        settings = self.store.get_weather()
        self.fetch.side_effect = WeatherError('weather_unavailable')
        self.service.refresh_once()
        self.assertEqual(self.service.snapshot()['status'], 'unavailable')
        for body in (dict(SETTINGS, place='90210'), dict(SETTINGS, units='k')):
            with self.assertRaises(WeatherError):
                self.service.configure(body)
            self.assertEqual(self.store.get_weather(), settings)

    def test_place_and_unit_changes_drop_cache_immediately(self):
        self.enable()
        self.service.refresh_once()
        self.service.configure(dict(SETTINGS, units='c'))
        self.assertIsNone(self.service.snapshot()['forecast'])
        self.service.refresh_once()
        self.assertEqual(self.service.snapshot()['status'], 'ok')
        self.service.configure(dict(SETTINGS, place='90210', units='c'))
        self.assertIsNone(self.service.snapshot()['forecast'])

    def test_old_fetch_is_discarded_when_configuration_changes(self):
        self.enable()
        entered, release = threading.Event(), threading.Event()
        def fetch(url):
            entered.set()
            if not release.wait(2):
                raise AssertionError('fetch was not released')
            return forecast_payload()
        self.service.fetch = fetch
        worker = threading.Thread(target=self.service.refresh_once, daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            self.service.configure(dict(SETTINGS, units='c'))
            self.assertEqual(self.service.snapshot()['status'], 'loading')
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertIsNone(self.service.snapshot()['forecast'])
        self.service.refresh_once()
        self.assertEqual(self.service.snapshot()['status'], 'ok')

    def test_old_place_fetch_cannot_set_the_new_places_timezone(self):
        self.enable()
        def fetch(url):
            if '/search?' in url:
                return PLACE
            self.service.configure(dict(SETTINGS, place='90210'))
            return dict(forecast_payload(), timezone='America/Chicago')
        self.service.fetch = fetch
        self.service.refresh_once()
        snapshot = self.service.snapshot()
        self.assertEqual(snapshot['place'], '90210')
        self.assertIsNone(snapshot['forecast'])
        self.assertIsNone(snapshot['timezone'])

    def test_timezone_changes_only_with_a_successful_forecast(self):
        self.enable()
        self.service.fetch = Mock(return_value=dict(forecast_payload(), timezone='America/Chicago'))
        self.service.refresh_once()
        self.assertEqual(self.service.snapshot()['timezone'], 'America/Chicago')
        self.now += 900
        self.service.fetch.return_value = {'timezone': 'America/Los_Angeles'}
        self.service.refresh_once()
        self.assertEqual(self.service.snapshot()['timezone'], 'America/Chicago')
        self.now += 120
        self.service.fetch.return_value = dict(forecast_payload(), timezone='America/Los_Angeles')
        self.service.refresh_once()
        self.assertEqual(self.service.snapshot()['timezone'], 'America/Los_Angeles')

    def test_timezone_is_memory_only_and_cleared_by_a_disabled_place_change(self):
        self.enable()
        self.service.fetch = Mock(return_value=dict(forecast_payload(), timezone='America/Chicago'))
        self.service.refresh_once()
        self.assertNotIn('timezone', self.store.get_weather())
        restarted = WeatherService(self.store, self.fetch, lambda: self.now)
        self.addCleanup(restarted.stop)
        self.assertIsNone(restarted.snapshot()['timezone'])
        self.service.configure(dict(SETTINGS, enabled=False, place='90210'))
        self.assertIsNone(self.service.snapshot()['timezone'])
        self.service.fetch.return_value = PLACE
        self.assertIsNone(self.service.configure(dict(SETTINGS, place='90210'))['timezone'])

    def test_geocode_does_not_hold_store_or_snapshot_locks(self):
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def fetch(url):
            entered.set()
            if not release.wait(2):
                raise AssertionError('geocode was not released')
            return PLACE
        self.service.fetch = fetch
        def configure():
            try:
                self.service.configure(SETTINGS)
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=configure, daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            def mutate():
                self.store.set_layout('2x2')
                self.service.snapshot()
                completed.set()
            mutation = threading.Thread(target=mutate, daemon=True)
            mutation.start()
            self.assertTrue(completed.wait(1))
        finally:
            release.set()
            worker.join(2)
        self.assertEqual(errors, [])
        self.assertEqual(self.store.snapshot()['layout'], '2x2')

    def test_worker_wait_schedule_with_fake_event_and_clock(self):
        self.enable()
        service = self.service
        waits = []
        class Wake:
            def clear(self):
                pass
            def set(self):
                pass
            def wait(inner, delay):
                waits.append(delay)
                self.now += delay
                if len(waits) == 1:
                    self.fetch.side_effect = WeatherError('weather_unavailable')
                if len(waits) == 2:
                    service._stop.set()
        service._wake = Wake()
        service._run()
        self.assertEqual(waits, [900, 120])

    def test_worker_is_daemon_and_stop_wakes_idle_thread(self):
        self.service.start()
        thread = self.service._thread
        self.assertTrue(thread.daemon)
        self.service.stop()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.fetch.assert_not_called()

    def test_stop_between_worker_loop_and_event_clear_never_waits(self):
        service = self.service
        class Wake:
            def clear(self):
                service._stop.set()
            def set(self):
                pass
            def wait(self, delay):
                raise AssertionError('stopped worker must not wait')
        service._wake = Wake()
        service._run()
        self.fetch.assert_not_called()

    def test_stop_during_network_discards_result(self):
        self.enable()
        entered, release = threading.Event(), threading.Event()
        def fetch(url):
            entered.set()
            release.wait(2)
            return forecast_payload()
        self.service.fetch = fetch
        self.service.start()
        try:
            self.assertTrue(entered.wait(2))
            self.service.stop()
        finally:
            release.set()
            self.service._thread.join(2)
        self.assertFalse(self.service._thread.is_alive())
        self.assertIsNone(self.service.snapshot()['forecast'])
