"""Private, atomic JSON persistence. No camera login is stored here."""

import copy
import datetime
import json
import os
from pathlib import Path
import tempfile
import threading
import unicodedata

from .discovery import normalize_mac
from .envfile import split_csv
from .security import CAMERA_ID

MAX_CAMERAS = 16
LAYOUTS = ("auto", "2x1", "2x2", "3x3", "4x4", "spotlight")


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


def validate_state(state):
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
    if set(state) != {"version", "next_id", "layout", "cameras"}:
        raise ValueError("Unexpected state fields")
    return copy.deepcopy(state)


class CameraStore:
    def __init__(self, path, seed=None, warning=print):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        if self.path.exists():
            os.chmod(self.path, 0o600)
            try:
                self._state = validate_state(json.loads(self.path.read_text(encoding="utf-8")))
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

    def restore(self, state):
        with self.lock:
            state = validate_state(state)
            state["next_id"] = max(state["next_id"], self._state["next_id"])
            self._commit(state)

    def check_add(self, mac, name=None):
        with self.lock:
            mac = normalize_mac(mac)
            if not mac:
                raise StoreError("invalid_mac", "Enter a valid camera MAC address.")
            if any(camera["mac"] == mac for camera in self._state["cameras"]):
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
            self._commit(state)

    def set_layout(self, layout):
        with self.lock:
            if layout not in LAYOUTS:
                raise StoreError("invalid_layout", "Choose auto, 2x1, 2x2, 3x3, 4x4, or spotlight.")
            state = self.snapshot()
            state["layout"] = layout
            self._commit(state)
            return layout
