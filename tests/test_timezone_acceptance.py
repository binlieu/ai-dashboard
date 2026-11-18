"""Acceptance tests for the weather place's time zone (orchestrator-written; do not modify).

The clock follows the time zone of the weather place. The zone name comes from the forecast
response, is validated before it reaches the page, and never depends on the forecast cache.
"""

import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from app.store import CameraStore
from app.weather import WeatherService, parse_forecast, parse_timezone

PLACE = {"results": [{"name": "Springfield", "admin1": "Illinois", "country": "United States",
                      "latitude": 39.80172, "longitude": -89.64371}]}
OTHER_PLACE = {"results": [{"name": "Beverly Hills", "admin1": "California",
                            "country": "United States", "latitude": 34.09, "longitude": -118.41}]}
SETTINGS = {"enabled": True, "place": "Springfield", "units": "f"}


def forecast_payload(**extra):
    payload = {"current": {"temperature_2m": 72, "apparent_temperature": 70, "weather_code": 0,
                           "wind_speed_10m": 8, "is_day": 1},
               "daily": {"time": ["2026-11-%s" % day for day in range(18, 23)],
                         "weather_code": [0, 2, 3, 63, 71], "temperature_2m_max": [75] * 5,
                         "temperature_2m_min": [50] * 5,
                         "precipitation_probability_max": [0, 10, 20, 60, None]}}
    payload.update(extra)
    return payload


class ParseTimezone(unittest.TestCase):
    def test_valid_names(self):
        for name in ("America/Chicago", "America/Los_Angeles", "America/Argentina/Buenos_Aires",
                     "America/Port-au-Prince", "Asia/Ho_Chi_Minh", "Etc/GMT+5", "UTC", "GMT"):
            self.assertEqual(parse_timezone({"timezone": name}), name)

    def test_missing_or_unusable_names_give_none(self):
        bad = [None, [], "America/Chicago", {}, {"timezone": None}, {"timezone": 5},
               {"timezone": True}, {"timezone": ""}, {"timezone": " America/Chicago"},
               {"timezone": "America/Chicago\n"}, {"timezone": "America/Chi cago"},
               {"timezone": "<script>"}, {"timezone": "America/‮Chicago"},
               {"timezone": "/America"}, {"timezone": "America/"}, {"timezone": "A" * 65},
               {"timezone": "América/Chicago"}, {"timezone": ["America/Chicago"]}]
        for payload in bad:
            self.assertIsNone(parse_timezone(payload), repr(payload))

    def test_never_raises_and_does_not_change_the_forecast_shape(self):
        payload = forecast_payload(timezone="America/Chicago")
        self.assertEqual(set(parse_forecast(payload)), {"current", "days"})


class ServiceTimezone(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = CameraStore(Path(directory.name) / "data/cameras.json")
        self.now = 100000
        self.zone = "America/Chicago"
        self.place = PLACE
        self.fail = False

        def fetch(url):
            if "/search?" in url:
                return copy.deepcopy(self.place)
            if self.fail:
                raise OSError("offline")
            return forecast_payload(timezone=self.zone) if self.zone is not _ABSENT else forecast_payload()

        self.fetch = Mock(side_effect=fetch)
        self.service = WeatherService(self.store, self.fetch, lambda: self.now)
        self.addCleanup(self.service.stop)

    def test_none_until_a_forecast_arrives(self):
        self.assertIsNone(self.service.snapshot()["timezone"])
        self.assertIsNone(self.service.configure(SETTINGS)["timezone"])
        self.service.refresh_once()
        self.assertEqual(self.service.snapshot()["timezone"], "America/Chicago")

    def test_kept_while_the_forecast_fails_or_expires(self):
        self.service.configure(SETTINGS)
        self.service.refresh_once()
        self.fail = True
        self.now += 4 * 3600
        snapshot = self.service.snapshot()
        self.assertIsNone(snapshot["forecast"])
        self.assertEqual(snapshot["timezone"], "America/Chicago")
        self.service.refresh_once()
        self.assertEqual(self.service.snapshot()["status"], "unavailable")
        self.assertEqual(self.service.snapshot()["timezone"], "America/Chicago")

    def test_kept_when_only_the_units_change(self):
        self.service.configure(SETTINGS)
        self.service.refresh_once()
        self.assertEqual(self.service.configure(dict(SETTINGS, units="c"))["timezone"], "America/Chicago")

    def test_cleared_when_the_place_changes_then_follows_the_new_place(self):
        self.service.configure(SETTINGS)
        self.service.refresh_once()
        self.place, self.zone = OTHER_PLACE, "America/Los_Angeles"
        self.assertIsNone(self.service.configure(dict(SETTINGS, place="90210"))["timezone"])
        self.service.refresh_once()
        self.assertEqual(self.service.snapshot()["timezone"], "America/Los_Angeles")

    def test_hidden_while_the_tile_is_off_and_back_when_it_returns(self):
        self.service.configure(SETTINGS)
        self.service.refresh_once()
        self.assertIsNone(self.service.configure(dict(SETTINGS, enabled=False))["timezone"])
        self.assertEqual(self.service.configure(SETTINGS)["timezone"], "America/Chicago")

    def test_a_forecast_without_a_usable_zone_keeps_the_last_known_one(self):
        self.service.configure(SETTINGS)
        self.service.refresh_once()
        self.zone = "not a zone"
        self.now += 1000
        self.service.refresh_once()
        self.assertEqual(self.service.snapshot()["status"], "ok")
        self.assertEqual(self.service.snapshot()["timezone"], "America/Chicago")
        self.zone = _ABSENT
        self.now += 1000
        self.service.refresh_once()
        self.assertEqual(self.service.snapshot()["timezone"], "America/Chicago")

    def test_coordinates_stay_private(self):
        self.service.configure(SETTINGS)
        self.service.refresh_once()
        self.assertEqual(set(self.service.snapshot()),
                         {"enabled", "place", "units", "name", "forecast", "status", "timezone"})


_ABSENT = object()

if __name__ == "__main__":
    unittest.main()
