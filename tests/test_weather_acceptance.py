"""Acceptance tests for the weather feature. Written by the orchestrator — do NOT modify.

Run from the repo root:  /usr/bin/python3 -m unittest tests.test_weather_acceptance -v

Part 1 — request gate (app/security.py, existing contract of check_request):
    GET /api/weather   read settings + cached forecast (same rules as GET /api/cameras)
    PUT /api/weather   change settings (same rules as PUT /api/layout)
    Nothing else under /api/weather is reachable.

Part 2 — pure functions in app/weather.py (no network, no files):

    class WeatherError(Exception)       has .code (str) and a safe message

    validate_settings(body) -> {"enabled": bool, "place": str, "units": "f" | "c"}
    build_geocode_url(place) -> str
    parse_geocode(payload) -> {"name": str, "lat": float, "lon": float}
    build_forecast_url(lat, lon, units) -> str
    parse_forecast(payload) -> {"current": {...}, "days": [...]}
"""

import math
import unittest
from urllib.parse import parse_qs, urlsplit

from app.security import check_request
from app.weather import (WeatherError, build_forecast_url, build_geocode_url, parse_forecast,
                         parse_geocode, validate_settings)

PORT = 1984
HOST = "127.0.0.1:1984"
ORIGIN = "http://127.0.0.1:1984"
JSON = "application/json"


def h(**extra):
    headers = {"host": HOST}
    for key, value in extra.items():
        headers[key.replace("_", "-")] = value
    return headers


class Gate(unittest.TestCase):
    def test_read_allowed(self):
        self.assertEqual(check_request("GET", "/api/weather", h(), PORT), 200)
        self.assertEqual(
            check_request("GET", "/api/weather", h(sec_fetch_site="same-origin"), PORT), 200)

    def test_read_rejected_from_other_sites(self):
        self.assertEqual(
            check_request("GET", "/api/weather", h(sec_fetch_site="cross-site"), PORT), 403)
        self.assertEqual(
            check_request("GET", "/api/weather", h(sec_fetch_site="same-site"), PORT), 403)
        self.assertEqual(
            check_request("GET", "/api/weather", h(origin="https://evil.example"), PORT), 403)
        self.assertEqual(
            check_request("GET", "/api/weather", {"host": "evil.example:1984"}, PORT), 403)

    def test_write_allowed_only_same_origin_json(self):
        ok = h(origin=ORIGIN, content_type=JSON)
        self.assertEqual(check_request("PUT", "/api/weather", ok, PORT), 200)
        self.assertEqual(
            check_request("PUT", "/api/weather",
                          h(origin=ORIGIN, content_type="application/json; charset=utf-8"),
                          PORT), 200)
        self.assertEqual(check_request("PUT", "/api/weather", h(content_type=JSON), PORT), 403)
        for origin in ("https://evil.example", "null", "http://127.0.0.1:1985",
                       "http://localhost:1984"):
            self.assertEqual(
                check_request("PUT", "/api/weather", h(origin=origin, content_type=JSON), PORT),
                403, origin)
        for content_type in ("text/plain", "application/x-www-form-urlencoded", ""):
            self.assertEqual(
                check_request("PUT", "/api/weather",
                              h(origin=ORIGIN, content_type=content_type), PORT),
                415, repr(content_type))
        self.assertEqual(check_request("PUT", "/api/weather", h(origin=ORIGIN), PORT), 415)

    def test_other_methods_rejected(self):
        ok = h(origin=ORIGIN, content_type=JSON)
        for method in ("POST", "PATCH", "DELETE", "TRACE"):
            self.assertIn(check_request(method, "/api/weather", ok, PORT), (403, 404, 405), method)

    def test_nothing_else_under_weather(self):
        ok = h(origin=ORIGIN, content_type=JSON)
        for target in ("/api/weather/", "/api/weather/search", "/api/weather/search?q=x",
                       "/api/weather?lat=1&lon=2", "/api/weather?", "/api/weatherx",
                       "/api/weather%2f..", "/api/weather/../config", "/api/Weather"):
            for method in ("GET", "PUT"):
                self.assertIn(check_request(method, target, ok, PORT), (403, 404),
                              "%s %s" % (method, target))


class Settings(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(
            validate_settings({"enabled": True, "place": "  Springfield, IL ", "units": "f"}),
            {"enabled": True, "place": "Springfield, IL", "units": "f"})
        self.assertEqual(
            validate_settings({"enabled": False, "place": "", "units": "c"}),
            {"enabled": False, "place": "", "units": "c"})

    def test_place_optional_only_when_disabled(self):
        self.assertEqual(validate_settings({"enabled": False, "units": "f"})["place"], "")
        for body in ({"enabled": True, "units": "f"}, {"enabled": True, "place": "  ", "units": "f"}):
            with self.assertRaises(WeatherError) as caught:
                validate_settings(body)
            self.assertEqual(caught.exception.code, "invalid_place")

    def test_invalid_bodies(self):
        bad = [
            None, [], "x", 5,
            {},
            {"enabled": "yes", "place": "A", "units": "f"},
            {"enabled": 1, "place": "A", "units": "f"},
            {"enabled": True, "place": 5, "units": "f"},
            {"enabled": True, "place": "A" * 81, "units": "f"},
            {"enabled": True, "place": "A\nB", "units": "f"},
            {"enabled": True, "place": "A\x00B", "units": "f"},
            {"enabled": True, "place": "A\x1b[2J", "units": "f"},
            {"enabled": True, "place": "A", "units": "k"},
            {"enabled": True, "place": "A", "units": "F"},
            {"enabled": True, "place": "A"},
            {"enabled": True, "place": "A", "units": "f", "lat": 1},
        ]
        for body in bad:
            with self.assertRaises(WeatherError, msg=repr(body)):
                validate_settings(body)


class GeocodeUrl(unittest.TestCase):
    def test_fixed_host_and_encoded_place(self):
        place = "A&count=50&name=evil#frag /x?y=1 é"
        parts = urlsplit(build_geocode_url(place))
        self.assertEqual((parts.scheme, parts.netloc, parts.path),
                         ("https", "geocoding-api.open-meteo.com", "/v1/search"))
        self.assertEqual(parts.fragment, "")
        query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=True)
        self.assertEqual(query, {"name": [place], "count": ["1"], "language": ["en"],
                                 "format": ["json"]})


    def test_five_digit_zip_is_searched_in_the_us_only(self):
        # A bare ZIP such as 10001 also matches postcodes in other countries.
        for place in ("90210", " 10001 "):
            query = parse_qs(urlsplit(build_geocode_url(place)).query, strict_parsing=True)
            self.assertEqual(query, {"name": [place.strip()], "count": ["1"], "language": ["en"],
                                     "format": ["json"], "countryCode": ["US"]})
        for place in ("9021", "902101", "90210 CA", "SW1A 1AA", "Paris 75001"):
            query = parse_qs(urlsplit(build_geocode_url(place)).query, strict_parsing=True)
            self.assertNotIn("countryCode", query, place)


class Geocode(unittest.TestCase):
    RESULT = {"id": 1, "name": "Springfield", "latitude": 39.80172, "longitude": -89.64371,
              "country": "United States", "admin1": "Illinois", "admin2": "Sangamon"}

    def test_name_and_rounded_coordinates(self):
        self.assertEqual(parse_geocode({"results": [self.RESULT], "generationtime_ms": 0.2}),
                         {"name": "Springfield, Illinois, United States", "lat": 39.8, "lon": -89.64})

    def test_missing_or_repeated_parts_are_skipped(self):
        only_name = {"name": "Atlantis", "latitude": 1, "longitude": 2}
        self.assertEqual(parse_geocode({"results": [only_name]}),
                         {"name": "Atlantis", "lat": 1.0, "lon": 2.0})
        city_state = dict(self.RESULT, name="Singapore", admin1="Singapore", country="Singapore")
        self.assertEqual(parse_geocode({"results": [city_state]})["name"], "Singapore")

    def test_name_is_cleaned_and_bounded(self):
        dirty = dict(self.RESULT, name="Spring\x1b[31m\nfield", admin1="I" * 300)
        name = parse_geocode({"results": [dirty]})["name"]
        self.assertNotIn("\x1b", name)
        self.assertNotIn("\n", name)
        self.assertLessEqual(len(name), 120)

    def test_not_found(self):
        for payload in ({"generationtime_ms": 0.1}, {"results": []}, {"results": None}):
            with self.assertRaises(WeatherError) as caught:
                parse_geocode(payload)
            self.assertEqual(caught.exception.code, "place_not_found", repr(payload))

    def test_malformed(self):
        bad = [None, [], "x", {"results": "x"}, {"results": [5]}, {"results": [{}]},
               {"results": [dict(self.RESULT, latitude="39.8")]},
               {"results": [dict(self.RESULT, latitude=True)]},
               {"results": [dict(self.RESULT, latitude=91)]},
               {"results": [dict(self.RESULT, longitude=-181)]},
               {"results": [dict(self.RESULT, longitude=float("nan"))]},
               {"results": [dict(self.RESULT, name="")]},
               {"results": [dict(self.RESULT, name=5)]}]
        for payload in bad:
            with self.assertRaises(WeatherError) as caught:
                parse_geocode(payload)
            self.assertEqual(caught.exception.code, "weather_unavailable", repr(payload))


class ForecastUrl(unittest.TestCase):
    def query(self, *args):
        parts = urlsplit(build_forecast_url(*args))
        self.assertEqual((parts.scheme, parts.netloc, parts.path),
                         ("https", "api.open-meteo.com", "/v1/forecast"))
        return parse_qs(parts.query, keep_blank_values=True, strict_parsing=True)

    def test_fahrenheit(self):
        query = self.query(39.8, -89.64, "f")
        self.assertEqual(query["latitude"], ["39.80"])
        self.assertEqual(query["longitude"], ["-89.64"])
        self.assertEqual(query["temperature_unit"], ["fahrenheit"])
        self.assertEqual(query["wind_speed_unit"], ["mph"])
        self.assertEqual(query["forecast_days"], ["5"])
        self.assertEqual(query["timezone"], ["auto"])
        self.assertEqual(set(query["current"][0].split(",")),
                         {"temperature_2m", "apparent_temperature", "weather_code",
                          "wind_speed_10m", "is_day"})
        self.assertEqual(set(query["daily"][0].split(",")),
                         {"weather_code", "temperature_2m_max", "temperature_2m_min",
                          "precipitation_probability_max"})

    def test_celsius(self):
        query = self.query(1, 2, "c")
        self.assertEqual(query["temperature_unit"], ["celsius"])
        self.assertEqual(query["wind_speed_unit"], ["kmh"])
        self.assertEqual(query["latitude"], ["1.00"])

    def test_coordinates_are_never_more_precise_than_two_decimals(self):
        query = self.query(39.80172345, -89.64371999, "f")
        self.assertEqual(query["latitude"], ["39.80"])
        self.assertEqual(query["longitude"], ["-89.64"])

    def test_bad_arguments(self):
        for args in ((91, 0, "f"), (0, 181, "f"), ("1", 2, "f"), (True, 2, "f"), (None, 2, "f"),
                     (float("nan"), 2, "f"), (1, float("inf"), "f"), (1, 2, "k"), (1, 2, None),
                     ("1&x=1", 2, "f")):
            with self.assertRaises((ValueError, TypeError), msg=repr(args)):
                build_forecast_url(*args)


def forecast(**changes):
    payload = {
        "latitude": 39.8, "longitude": -89.64, "timezone": "America/Chicago",
        "current": {"time": "2026-11-18T10:30", "interval": 900, "temperature_2m": 51.8,
                    "apparent_temperature": 47.6, "weather_code": 3, "wind_speed_10m": 7.6,
                    "is_day": 1},
        "daily": {"time": ["2026-11-18", "2026-11-19", "2026-11-20", "2026-11-21", "2026-11-22"],
                  "weather_code": [53, 63, 51, 3, 0],
                  "temperature_2m_max": [53.6, 49.8, 49.4, 45.2, 47.4],
                  "temperature_2m_min": [48.8, 45.4, 43.4, 36.7, -3.6],
                  "precipitation_probability_max": [74, 100, 39, None, 0]},
    }
    for path, value in changes.items():
        section, key = path.split("__")
        if value is DROP:
            del payload[section][key]
        else:
            payload[section][key] = value
    return payload


DROP = object()


class Forecast(unittest.TestCase):
    def test_normalised_shape(self):
        self.assertEqual(parse_forecast(forecast()), {
            "current": {"temp": 52, "feels": 48, "code": 3, "wind": 8, "is_day": True},
            "days": [
                {"date": "2026-11-18", "code": 53, "high": 54, "low": 49, "rain": 74},
                {"date": "2026-11-19", "code": 63, "high": 50, "low": 45, "rain": 100},
                {"date": "2026-11-20", "code": 51, "high": 49, "low": 43, "rain": 39},
                {"date": "2026-11-21", "code": 3, "high": 45, "low": 37, "rain": None},
                {"date": "2026-11-22", "code": 0, "high": 47, "low": -4, "rain": 0},
            ],
        })

    def test_night_flag(self):
        self.assertIs(parse_forecast(forecast(current__is_day=0))["current"]["is_day"], False)

    def test_extra_days_are_cut_to_five(self):
        payload = forecast()
        for key, value in (("time", "2026-11-23"), ("weather_code", 1),
                           ("temperature_2m_max", 50), ("temperature_2m_min", 40),
                           ("precipitation_probability_max", 5)):
            payload["daily"][key] = payload["daily"][key] + [value, value]
        self.assertEqual(len(parse_forecast(payload)["days"]), 5)

    def test_values_are_plain_ints(self):
        result = parse_forecast(forecast())
        for key in ("temp", "feels", "code", "wind"):
            self.assertIs(type(result["current"][key]), int, key)
        for day in result["days"]:
            self.assertIs(type(day["high"]), int)
            self.assertIs(type(day["low"]), int)

    def test_hot_cold_and_windy_values_are_accepted(self):
        # Limits must fit both unit systems: temperatures -130..160, wind 0..300.
        payload = forecast(current__temperature_2m=120, current__apparent_temperature=-60,
                           current__wind_speed_10m=150,
                           daily__temperature_2m_max=[120, 105.3, 49.4, 45.2, 47.4],
                           daily__temperature_2m_min=[48.8, 45.4, 43.4, -60, -3.6])
        result = parse_forecast(payload)
        self.assertEqual((result["current"]["temp"], result["current"]["feels"],
                          result["current"]["wind"]), (120, -60, 150))
        self.assertEqual(result["days"][1]["high"], 105)
        self.assertEqual(result["days"][3]["low"], -60)
        for bad in (forecast(current__temperature_2m=161), forecast(current__temperature_2m=-131),
                    forecast(current__wind_speed_10m=301)):
            with self.assertRaises(WeatherError):
                parse_forecast(bad)

    def test_rain_is_clamped(self):
        payload = forecast(daily__precipitation_probability_max=[-5, 250, 39, None, 0])
        self.assertEqual([day["rain"] for day in parse_forecast(payload)["days"]],
                         [0, 100, 39, None, 0])

    def test_rejected_payloads(self):
        bad = [
            None, [], "x", {}, {"error": True, "reason": "Latitude must be in range"},
            dict(forecast(), error=True),
            {"current": forecast()["current"]},
            {"daily": forecast()["daily"]},
            forecast(current__temperature_2m=DROP),
            forecast(current__temperature_2m="51.8"),
            forecast(current__temperature_2m=True),
            forecast(current__temperature_2m=float("nan")),
            forecast(current__temperature_2m=999),
            forecast(current__apparent_temperature=None),
            forecast(current__weather_code=100),
            forecast(current__weather_code=-1),
            forecast(current__weather_code=3.5),
            forecast(current__wind_speed_10m=-1),
            forecast(current__wind_speed_10m=float("inf")),
            forecast(current__is_day=2),
            forecast(current__is_day="1"),
            forecast(daily__time=DROP),
            forecast(daily__time=[]),
            forecast(daily__time=["2026-11-18"] * 4),
            forecast(daily__time=["2026-11-18", "<script>", "2026-11-20", "2026-11-21", "2026-11-22"]),
            forecast(daily__time=["2026-11-18", 5, "2026-11-20", "2026-11-21", "2026-11-22"]),
            forecast(daily__weather_code=[53, 63, 51, 3]),
            forecast(daily__weather_code=[53, 63, 51, 3, "0"]),
            forecast(daily__temperature_2m_max=[53.6, None, 49.4, 45.2, 47.4]),
            forecast(daily__temperature_2m_min=[48.8, 45.4, 43.4, 36.7, -500]),
            forecast(daily__precipitation_probability_max=[74, 100, 39, "x", 0]),
            forecast(daily__precipitation_probability_max="none"),
        ]
        for payload in bad:
            with self.assertRaises(WeatherError, msg=repr(payload)[:120]) as caught:
                parse_forecast(payload)
            self.assertEqual(caught.exception.code, "weather_unavailable")

    def test_error_message_does_not_echo_the_payload(self):
        with self.assertRaises(WeatherError) as caught:
            parse_forecast({"error": True, "reason": "SENTINEL <b>reason</b>"})
        self.assertNotIn("SENTINEL", str(caught.exception))
        self.assertFalse(math.isnan(0.0))


if __name__ == "__main__":
    unittest.main()
