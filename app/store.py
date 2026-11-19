"""Private, atomic JSON persistence. No camera login is stored here."""

import copy
import datetime
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import unicodedata

from .backup import BACKUP_DEFAULTS, valid_folder_text, valid_policies
from .discovery import normalize_mac
from .envfile import split_csv
from .security import CAMERA_ID, DEVICE_ID

MAX_CAMERAS = 16
MAX_DEVICES = 6
LAYOUTS = ("auto", "2x1", "2x2", "3x3", "4x4", "spotlight")
WEATHER_DEFAULTS = {"enabled": False, "place": "", "units": "f", "name": "", "lat": None, "lon": None}


def validate_backup(backup, camera_ids):
    if not isinstance(backup, dict) or set(backup) != set(BACKUP_DEFAULTS):
        raise ValueError("Invalid backup settings")
    folder, cameras = backup["folder"], backup["cameras"]
    if (not valid_folder_text(folder) or not valid_policies(cameras) or
            not cameras.keys() <= camera_ids or (cameras and not folder)):
        raise ValueError("Invalid backup settings")
    return copy.deepcopy(backup)


def validate_weather(weather):
    if not isinstance(weather, dict) or set(weather) != set(WEATHER_DEFAULTS):
        raise ValueError("Invalid weather settings")
    if type(weather["enabled"]) is not bool or weather["units"] not in ("f", "c"):
        raise ValueError("Invalid weather settings")
    for key, limit in (("place", 80), ("name", 120)):
        text = weather[key]
        if not isinstance(text, str) or len(text) > limit or text != text.strip() or any(
                unicodedata.category(char).startswith("C") for char in text):
            raise ValueError("Invalid weather text")
    lat, lon = weather["lat"], weather["lon"]
    if lat is not None or lon is not None:
        for value, limit in ((lat, 90), (lon, 180)):
            if type(value) not in (int, float) or not -limit <= value <= limit or not math.isfinite(value):
                raise ValueError("Invalid weather coordinates")
    if weather["enabled"] and (not weather["place"] or not weather["name"] or lat is None or lon is None):
        raise ValueError("Weather needs a resolved place")
    return copy.deepcopy(weather)


class StoreError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def validate_name(name):
    if not isinstance(name, str):
        raise StoreError("invalid_name", "Camera name must be text.")
    if any(unicodedata.category(char) == "Cc" for char in name):
        raise StoreError("invalid_name", "Camera name must contain no control characters.")
    name = name.strip()
    if not 1 <= len(name) <= 40:
        raise StoreError("invalid_name", "Camera name must contain 1–40 characters and no controls.")
    return name


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def seed_state(values):
    text = values.get("CAMERA_MACS")
    if text is None:
        text = values.get("CAMERA_MAC") or ""
        if "," in text:
            raise StoreError("invalid_mac", "Use CAMERA_MACS for a list of cameras.")
    macs = split_csv(text) if text.strip() else []
    names = split_csv(values.get("CAMERA_NAMES") or "")
    if len(macs) > MAX_CAMERAS:
        raise StoreError("limit", "At most 16 cameras can be configured.", 409)
    cameras, seen = [], set()
    for index, mac in enumerate(macs, 1):
        normalized = normalize_mac(mac)
        if not normalized:
            raise StoreError("invalid_mac", "Invalid camera MAC at entry %s." % index)
        if normalized in seen:
            raise StoreError("duplicate", "Duplicate camera MAC at entry %s." % index, 409)
        seen.add(normalized)
        name = names[index - 1] if index <= len(names) else ""
        cameras.append({"id": "cam%s" % index, "name": validate_name(name or "Camera %s" % index),
                        "mac": normalized})
    return {"version": 1, "next_id": len(cameras) + 1, "layout": "auto", "cameras": cameras}


def validate_state(state, warning=None):
    if not isinstance(state, dict) or type(state.get("version")) is not int or state.get("version") != 1 or state.get("layout") not in LAYOUTS:
        raise ValueError("Invalid state")
    next_id = state.get("next_id")
    if type(next_id) is not int or not 1 <= next_id <= 10000:
        raise ValueError("Invalid next ID")
    cameras = state.get("cameras")
    if not isinstance(cameras, list) or len(cameras) > MAX_CAMERAS:
        raise ValueError("Invalid camera list")
    ids, macs = set(), set()
    for camera in cameras:
        if not isinstance(camera, dict) or not isinstance(camera.get("id"), str) or not CAMERA_ID.fullmatch(camera["id"]):
            raise ValueError("Invalid camera ID")
        mac = normalize_mac(camera.get("mac"))
        if not mac or mac != camera["mac"] or camera["id"] in ids or mac in macs:
            raise ValueError("Invalid camera MAC")
        if int(camera["id"][3:]) >= next_id or validate_name(camera.get("name")) != camera["name"]:
            raise ValueError("Invalid camera state")
        if set(camera) != {"id", "mac", "name"}:
            raise ValueError("Unexpected camera fields")
        ids.add(camera["id"])
        macs.add(mac)
    if set(state) - {"version", "next_id", "layout", "cameras", "weather", "devices", "next_device_id", "backup"}:
        raise ValueError("Unexpected state fields")
    state = copy.deepcopy(state)
    if "weather" in state:
        try:
            state["weather"] = validate_weather(state["weather"])
        except (ValueError, TypeError, OverflowError):
            state["weather"] = WEATHER_DEFAULTS.copy()
            if warning:
                warning("Weather settings were invalid and have been reset; cameras were preserved.")
    if "devices" in state or "next_device_id" in state:
        try:
            _validate_devices(state.get("devices"), state.get("next_device_id"), macs)
        except (ValueError, TypeError, OverflowError):
            state["devices"], state["next_device_id"] = [], 1
            if warning:
                warning("Device settings were invalid and have been reset; cameras were preserved.")
    if "backup" in state:
        try:
            state["backup"] = validate_backup(state["backup"], ids)
        except ValueError:
            state["backup"] = copy.deepcopy(BACKUP_DEFAULTS)
            if warning:
                warning("Backup settings were invalid and have been reset; cameras were preserved.")
    return state


def _validate_devices(devices, next_id, camera_macs):
    if type(next_id) is not int or next_id < 1:
        raise ValueError("Invalid next device ID")
    if not isinstance(devices, list) or len(devices) > MAX_DEVICES:
        raise ValueError("Invalid device list")
    ids, macs = set(), set(camera_macs)
    for device in devices:
        if (not isinstance(device, dict) or set(device) != {"id", "mac", "name"} or
                not isinstance(device["id"], str) or not DEVICE_ID.fullmatch(device["id"])):
            raise ValueError("Invalid device fields")
        mac = normalize_mac(device["mac"])
        if (not mac or mac != device["mac"] or mac in macs or device["id"] in ids or
                int(device["id"][3:]) >= next_id or validate_name(device["name"]) != device["name"]):
            raise ValueError("Invalid device state")
        ids.add(device["id"])
        macs.add(mac)


class CameraStore:
    def __init__(self, path, seed=None, warning=print):
        self.path = Path(path)
        self.lock = threading.RLock()
        self._backup_revision = 0
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        if self.path.exists():
            os.chmod(self.path, 0o600)
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                self._state = validate_state(loaded, warning)
                if (("weather" in loaded and loaded["weather"] != self._state["weather"]) or
                        ("backup" in loaded and loaded["backup"] != self._state["backup"]) or
                        ("devices" in self._state and
                         (loaded.get("devices") != self._state["devices"] or
                          type(loaded.get("next_device_id")) is not int or
                          loaded.get("next_device_id") != self._state["next_device_id"]))):
                    self._save(self._state)
                return
            except (ValueError, TypeError, KeyError, UnicodeError):
                suffix = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                os.replace(self.path, self.path.with_name(self.path.name + ".corrupt-" + suffix))
                warning("Camera state was corrupt; a private backup was kept and an empty list created.")
                self._state = seed_state({})
        else:
            values = seed() if callable(seed) else (seed or {})
            self._state = seed_state(values)
        self._save(self._state)

    def _save(self, state):
        atomic_write(self.path, json.dumps(state, ensure_ascii=True, indent=2) + "\n")

    def _commit(self, state):
        self._save(state)
        self._state = state

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self._state)

    def restore(self, state, *, backup_revision=None):
        with self.lock:
            state = validate_state(state)
            # Camera rollback preserves independent settings saves made during bridge I/O.
            if "weather" in self._state:
                state["weather"] = copy.deepcopy(self._state["weather"])
            if "devices" in self._state:
                state["devices"] = copy.deepcopy(self._state["devices"])
                state["next_device_id"] = self._state["next_device_id"]
                camera_macs = {camera["mac"] for camera in state["cameras"]}
                if any(device["mac"] in camera_macs for device in state["devices"]):
                    raise StoreError("duplicate", "That MAC address is already configured as a device.", 409)
            if "backup" in self._state and backup_revision != self._backup_revision:
                backup = copy.deepcopy(self._state["backup"])
                camera_ids = {camera["id"] for camera in state["cameras"]}
                backup["cameras"] = {camera_id: days for camera_id, days in backup["cameras"].items()
                                     if camera_id in camera_ids}
                state["backup"] = backup
            state["next_id"] = max(state["next_id"], self._state["next_id"])
            self._commit(state)

    def get_weather(self):
        with self.lock:
            return copy.deepcopy(self._state.get("weather", WEATHER_DEFAULTS))

    def set_weather(self, weather):
        weather = validate_weather(weather)
        with self.lock:
            state = self.snapshot()
            state["weather"] = weather
            self._commit(state)

    def get_backup(self):
        with self.lock:
            return copy.deepcopy(self._state.get("backup", BACKUP_DEFAULTS))

    def set_backup(self, backup):
        with self.lock:
            state = self.snapshot()
            state["backup"] = validate_backup(backup, {camera["id"] for camera in state["cameras"]})
            self._commit(state)
            self._backup_revision += 1
            return copy.deepcopy(state["backup"])

    def check_add(self, mac, name=None):
        with self.lock:
            mac = normalize_mac(mac)
            if not mac:
                raise StoreError("invalid_mac", "Enter a valid camera MAC address.")
            if any(camera["mac"] == mac for camera in self._state["cameras"]) or any(
                    device["mac"] == mac for device in self._state.get("devices", [])):
                raise StoreError("duplicate", "That camera is already configured.", 409)
            if len(self._state["cameras"]) >= MAX_CAMERAS or self._state["next_id"] > 9999:
                raise StoreError("limit", "The camera limit has been reached.", 409)
            index = self._state["next_id"]
            return {"id": "cam%s" % index, "mac": mac,
                    "name": validate_name("Camera %s" % index if name is None else name)}

    def add(self, mac, name=None):
        with self.lock:
            camera = self.check_add(mac, name)
            state = self.snapshot()
            state["cameras"].append(camera)
            state["next_id"] += 1
            self._commit(state)
            return copy.deepcopy(camera)

    def rename(self, camera_id, name):
        with self.lock:
            state = self.snapshot()
            for camera in state["cameras"]:
                if camera["id"] == camera_id:
                    camera["name"] = validate_name(name)
                    self._commit(state)
                    return copy.deepcopy(camera)
            raise StoreError("not_found", "Camera not found.", 404)

    def remove(self, camera_id):
        with self.lock:
            state = self.snapshot()
            if not any(camera["id"] == camera_id for camera in state["cameras"]):
                raise StoreError("not_found", "Camera not found.", 404)
            state["cameras"] = [camera for camera in state["cameras"] if camera["id"] != camera_id]
            if "backup" in state:
                state["backup"]["cameras"].pop(camera_id, None)
            self._commit(state)
            return self._backup_revision

    def set_layout(self, layout):
        with self.lock:
            if layout not in LAYOUTS:
                raise StoreError("invalid_layout", "Choose auto, 2x1, 2x2, 3x3, 4x4, or spotlight.")
            state = self.snapshot()
            state["layout"] = layout
            self._commit(state)
            return layout

    def get_devices(self):
        with self.lock:
            return copy.deepcopy(self._state.get("devices", []))

    def check_add_device(self, mac, name=None):
        with self.lock:
            mac = normalize_mac(mac)
            if not mac:
                raise StoreError("invalid_mac", "Enter a valid device MAC address.")
            if any(item["mac"] == mac for item in self._state["cameras"] + self._state.get("devices", [])):
                raise StoreError("duplicate", "That MAC address is already configured.", 409)
            index = self._state.get("next_device_id", 1)
            if len(self._state.get("devices", [])) >= MAX_DEVICES or index > 9999:
                raise StoreError("limit", "The device limit has been reached.", 409)
            return {"id": "dev%s" % index, "mac": mac,
                    "name": validate_name("Device %s" % index if name is None else name)}

    def add_device(self, mac, name=None):
        with self.lock:
            device = self.check_add_device(mac, name)
            state = self.snapshot()
            state.setdefault("devices", []).append(device)
            state["next_device_id"] = state.get("next_device_id", 1) + 1
            self._commit(state)
            return copy.deepcopy(device)

    def rename_device(self, device_id, name):
        with self.lock:
            state = self.snapshot()
            for device in state.get("devices", []):
                if device["id"] == device_id:
                    device["name"] = validate_name(name)
                    self._commit(state)
                    return copy.deepcopy(device)
            raise StoreError("not_found", "Device not found.", 404)

    def remove_device(self, device_id):
        with self.lock:
            state = self.snapshot()
            if not any(device["id"] == device_id for device in state.get("devices", [])):
                raise StoreError("not_found", "Device not found.", 404)
            state["devices"] = [device for device in state["devices"] if device["id"] != device_id]
            self._commit(state)
