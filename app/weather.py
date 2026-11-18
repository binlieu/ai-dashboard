"""Bounded server-side weather requests and a nonblocking forecast cache."""

import copy
import datetime
import json
import math
import re
import threading
import time
import unicodedata
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MESSAGES = {
    "invalid_settings": "Send enabled, place and units as weather settings.",
    "invalid_place": "Enter a ZIP code or city with at most 80 characters and no controls.",
    "invalid_units": "Choose Fahrenheit or Celsius.",
    "place_not_found": "Place not found. Try a ZIP code or another city name.",
    "weather_unavailable": "Weather is unavailable. Please try again later.",
}
HOSTS = {"geocoding-api.open-meteo.com", "api.open-meteo.com"}
MAX_RESPONSE = 256 * 1024


class WeatherError(Exception):
    def __init__(self, code, message=None):
        self.code = code
        self.message = MESSAGES[code]
        super().__init__(self.message)


def validate_settings(body):
    if not isinstance(body, dict) or set(body) - {"enabled", "place", "units"} or type(body.get("enabled")) is not bool:
        raise WeatherError("invalid_settings")
    place = body.get("place", "")
    if not isinstance(place, str) or any(unicodedata.category(char).startswith("C") for char in place):
        raise WeatherError("invalid_place")
    place = place.strip()
    if len(place) > 80 or (body["enabled"] and not place):
        raise WeatherError("invalid_place")
    if body.get("units") not in ("f", "c"):
        raise WeatherError("invalid_units")
    return {"enabled": body["enabled"], "place": place, "units": body["units"]}


def _number(value, low, high):
    return type(value) in (int, float) and low <= value <= high and math.isfinite(value)


def build_geocode_url(place):
    place = place.strip()
    query = {"name": place, "count": 1, "language": "en", "format": "json"}
    if re.fullmatch(r"[0-9]{5}", place):
        query["countryCode"] = "US"
    return "https://geocoding-api.open-meteo.com/v1/search?" + urlencode(query)


def parse_geocode(payload):
    if not isinstance(payload, dict) or payload.get("error"):
        raise WeatherError("weather_unavailable")
    results = payload.get("results")
    if results is None or results == []:
        raise WeatherError("place_not_found")
    if not isinstance(results, list) or not isinstance(results[0], dict):
        raise WeatherError("weather_unavailable")
    result = results[0]
    lat, lon = result.get("latitude"), result.get("longitude")
    if not _number(lat, -90, 90) or not _number(lon, -180, 180):
        raise WeatherError("weather_unavailable")
    parts = []
    for key in ("name", "admin1", "country"):
        value = result.get(key, "")
        if not isinstance(value, str):
            raise WeatherError("weather_unavailable")
        value = "".join(char for char in value if not unicodedata.category(char).startswith("C")).strip()
        if key == "name" and not value:
            raise WeatherError("weather_unavailable")
        if value and value not in parts:
            parts.append(value)
    name = ", ".join(parts)[:120].rstrip(" ,")
    if not name:
        raise WeatherError("weather_unavailable")
    return {"name": name, "lat": round(float(lat), 2), "lon": round(float(lon), 2)}


def build_forecast_url(lat, lon, units):
    if not _number(lat, -90, 90) or not _number(lon, -180, 180) or units not in ("f", "c"):
        raise ValueError("Invalid forecast arguments")
    query = {"latitude": "%.2f" % lat, "longitude": "%.2f" % lon,
             "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,is_day",
             "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
             "temperature_unit": "fahrenheit" if units == "f" else "celsius",
             "wind_speed_unit": "mph" if units == "f" else "kmh", "timezone": "auto", "forecast_days": 5}
    return "https://api.open-meteo.com/v1/forecast?" + urlencode(query)


def _rounded(value, low, high):
    if not _number(value, low, high):
        raise ValueError("Invalid weather number")
    return int(round(value))


def _code(value):
    if type(value) is not int or not 0 <= value <= 99:
        raise ValueError("Invalid weather code")
    return value


def parse_forecast(payload):
    try:
        if not isinstance(payload, dict) or payload.get("error"):
            raise ValueError("Invalid forecast")
        current, daily = payload["current"], payload["daily"]
        if type(current["is_day"]) is not int or current["is_day"] not in (0, 1):
            raise ValueError("Invalid day flag")
        result = {"current": {"temp": _rounded(current["temperature_2m"], -130, 160),
                              "feels": _rounded(current["apparent_temperature"], -130, 160),
                              "code": _code(current["weather_code"]),
                              "wind": _rounded(current["wind_speed_10m"], 0, 300),
                              "is_day": bool(current["is_day"])}, "days": []}
        keys = ("time", "weather_code", "temperature_2m_max", "temperature_2m_min", "precipitation_probability_max")
        if any(not isinstance(daily[key], list) or len(daily[key]) < 5 for key in keys):
            raise ValueError("Invalid daily arrays")
        for index in range(5):
            date = daily["time"][index]
            if not isinstance(date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                raise ValueError("Invalid date")
            datetime.date.fromisoformat(date)
            rain = daily["precipitation_probability_max"][index]
            if rain is not None:
                if type(rain) not in (int, float) or not math.isfinite(rain):
                    raise ValueError("Invalid precipitation")
                rain = int(round(max(0, min(100, rain))))
            result["days"].append({"date": date, "code": _code(daily["weather_code"][index]),
                                   "high": _rounded(daily["temperature_2m_max"][index], -130, 160),
                                   "low": _rounded(daily["temperature_2m_min"][index], -130, 160), "rain": rain})
        return result
    except (ValueError, TypeError, KeyError, IndexError, OverflowError):
        raise WeatherError("weather_unavailable") from None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        # Refuse all redirects, including redirects between the two allowed hosts.
        raise WeatherError("weather_unavailable")


def fetch_json(url, opener=None):
    try:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.netloc not in HOSTS or parts.fragment:
            raise ValueError("Weather host is not allowed")
        if opener is None:
            opener = build_opener(ProxyHandler({}), _NoRedirect()).open
        request = Request(url, headers={"User-Agent": "kasa-camera-dashboard"})
        with opener(request, timeout=10) as response:
            raw = response.read(MAX_RESPONSE)
            if len(raw) >= MAX_RESPONSE:
                raise ValueError("Response too large")
            return json.loads(raw.decode("utf-8"))
    except Exception:
        raise WeatherError("weather_unavailable") from None


class WeatherService:
    def __init__(self, store, fetch=fetch_json, clock=time.time):
        self.store, self.fetch, self.clock = store, fetch, clock
        self._lock = threading.RLock()
        self._configure_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._generation = 0
        self._forecast = None
        self._failed = False
        self._next_refresh = 0

    def snapshot(self):
        with self._lock:
            settings = self.store.get_weather()
            if self._forecast and self.clock() - self._forecast["updated"] > 3 * 3600:
                self._forecast = None
                self._failed = True
            forecast = copy.deepcopy(self._forecast) if settings["enabled"] else None
            if not settings["enabled"]:
                status = "off"
            elif forecast:
                status = "ok"
            else:
                status = "unavailable" if self._failed else "loading"
            result = {key: settings[key] for key in ("enabled", "place", "units", "name")}
            result.update(forecast=forecast, status=status)
            return result

    def configure(self, body):
        settings = validate_settings(body)
        # Only weather saves serialize here. Neither camera nor store locks cover network I/O.
        with self._configure_lock:
            previous = self.store.get_weather()
            changed_place = settings["place"] != previous["place"]
            location = {key: previous[key] for key in ("name", "lat", "lon")}
            if changed_place:
                location = {"name": "", "lat": None, "lon": None}
            if settings["enabled"] and (changed_place or location["lat"] is None or location["lon"] is None):
                location = parse_geocode(self.fetch(build_geocode_url(settings["place"])))
            with self._lock:
                self.store.set_weather(dict(settings, **location))
                self._generation += 1
                if changed_place or settings["units"] != previous["units"]:
                    self._forecast = None
                self._failed = False
                self._next_refresh = 0
                self._wake.set()
                return self.snapshot()

    def refresh_once(self):
        with self._lock:
            settings = self.store.get_weather()
            if self._stop.is_set() or not settings["enabled"] or self.clock() < self._next_refresh:
                return
            generation = self._generation
        try:
            forecast = parse_forecast(self.fetch(build_forecast_url(settings["lat"], settings["lon"], settings["units"])))
        except Exception:
            forecast = None
        with self._lock:
            if generation != self._generation or self._stop.is_set():
                return
            if forecast is not None:
                self._forecast = dict(forecast, updated=int(self.clock()))
            self._failed = forecast is None
            self._next_refresh = self.clock() + (120 if self._failed else 900)

    def _run(self):
        while not self._stop.is_set():
            self._wake.clear()
            if self._stop.is_set():
                break
            self.refresh_once()
            with self._lock:
                enabled = self.store.get_weather()["enabled"]
                delay = max(0, self._next_refresh - self.clock()) if enabled else None
            self._wake.wait(delay)

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="weather", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=0.2)
