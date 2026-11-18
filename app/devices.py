"""Local plug/switch operations and a cache that never waits on device I/O."""

from contextlib import contextmanager
import re
import threading
import time

from .discovery import ip_for_mac, sweep_local_network, valid_ip
from .identify import describe, identify_device
from .kasa_control import ControlError
from .store import MAX_DEVICES, StoreError


def _control_error(error, adding=False):
    if isinstance(error, ControlError) and error.code == "auth":
        return StoreError("auth", "The device rejected the Kasa account login.", 422)
    if adding and isinstance(error, ControlError) and error.code == "unsupported":
        return StoreError("not_device", "Multi-outlet devices are not supported yet.", 422)
    return StoreError("unreachable", "The device did not answer.", 502)


def _wrong_kind(identity):
    if not isinstance(identity, dict):
        return "This is not a Kasa plug or switch."
    kind = identity.get("kind")
    if kind == "camera":
        return "This is a camera. Add it under Cameras."
    if kind not in ("light", "router", "other"):
        return "This is not a Kasa plug or switch."
    model = identity.get("model")
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.+-]{0,23}", model):
        model = None
    return describe({"kind": kind, "model": model}).replace(", not a camera.", ", not a plug or switch.")


class DeviceService:
    def __init__(self, store, client, lookup=ip_for_mac, sweep=sweep_local_network,
                 identify=identify_device, clock=time.monotonic):
        self.store, self.client = store, client
        self.lookup, self.sweep, self.identify, self.clock = lookup, sweep, identify, clock
        self._lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self._pending_operations = 0
        self._states, self._ips, self._next_poll = {}, {}, {}
        self._wake, self._stop = threading.Event(), threading.Event()
        self._thread = None

    @contextmanager
    def _operation(self):
        # A queued button press takes priority over the next background device.
        with self._lock:
            self._pending_operations += 1
        try:
            with self._operation_lock:
                yield
        finally:
            with self._lock:
                self._pending_operations -= 1
                self._wake.set()

    def settings(self):
        with self._lock:
            return {"devices": [dict(device, state=self._states.get(device["id"], "unknown"))
                                for device in self.store.get_devices()], "max": MAX_DEVICES}

    def _device(self, device_id):
        # Callers hold the short state lock, never the camera application lock.
        for device in self.store.get_devices():
            if device["id"] == device_id:
                return device
        raise StoreError("not_found", "Device not found.", 404)

    def _lookup(self, mac):
        try:
            return valid_ip(self.lookup(mac))
        except Exception:
            return None

    def _remember_ip(self, device, ip):
        with self._lock:
            if any(item["id"] == device["id"] for item in self.store.get_devices()):
                if ip is None:
                    self._ips.pop(device["id"], None)
                else:
                    self._ips[device["id"]] = ip

    def _record(self, device_id, state):
        with self._lock:
            if any(device["id"] == device_id for device in self.store.get_devices()):
                self._states[device_id] = state
                self._next_poll[device_id] = self.clock() + (3 if state == "unknown" else 15)
                self._wake.set()

    def _exchange(self, device, on=None):
        with self._lock:
            ip = self._ips.get(device["id"])
        if ip is None:
            ip = self._lookup(device["mac"])
            self._remember_ip(device, ip)
        if ip is None:
            raise _control_error(None)
        try:
            state = self.client.read(ip) if on is None else self.client.set(ip, on)
            if type(state) is not bool:
                raise ControlError("protocol")
            return "on" if state else "off"
        except Exception as error:
            # Refresh for the next attempt; do not automatically repeat a write.
            self._remember_ip(device, self._lookup(device["mac"]))
            raise _control_error(error) from None

    def add(self, body):
        if not isinstance(body, dict) or set(body) - {"mac", "name"}:
            raise StoreError("invalid_device", "Send a device MAC address and optional name.")
        with self._operation():
            candidate = self.store.check_add_device(body.get("mac"), body.get("name"))
            ip = self._lookup(candidate["mac"])
            if ip is None:
                try:
                    self.sweep()
                except Exception:
                    pass
                ip = self._lookup(candidate["mac"])
            if ip is None:
                raise StoreError("not_found", "Device not found on this network.", 404)
            try:
                identity = self.identify(ip)
            except Exception:
                identity = None
            if not isinstance(identity, dict) or identity.get("kind") not in ("plug", "switch", "plug_or_switch"):
                raise StoreError("not_device", _wrong_kind(identity), 422)
            try:
                state = self.client.read(ip)
                if type(state) is not bool:
                    raise ControlError("protocol")
            except Exception as error:
                raise _control_error(error, adding=True) from None
            with self._lock:
                device = self.store.add_device(candidate["mac"], candidate["name"])
                self._ips[device["id"]] = ip
                self._record(device["id"], "on" if state else "off")
                return dict(device, state=self._states[device["id"]])

    def set_state(self, device_id, body):
        if not isinstance(body, dict) or set(body) != {"on"} or type(body["on"]) is not bool:
            raise StoreError("invalid_state", "Send exactly an on value of true or false.")
        with self._operation():
            with self._lock:
                device = self._device(device_id)
            try:
                state = self._exchange(device, body["on"])
            except StoreError:
                self._record(device_id, "unknown")
                raise
            with self._lock:
                # A rename/remove can complete while the device is answering.
                device = self._device(device_id)
                self._record(device_id, state)
                return {"id": device_id, "name": device["name"], "state": state}

    def rename(self, device_id, body):
        if not isinstance(body, dict) or set(body) != {"name"}:
            raise StoreError("invalid_name", "Send a device name.")
        with self._lock:
            device = self.store.rename_device(device_id, body["name"])
            return dict(device, state=self._states.get(device_id, "unknown"))

    def remove(self, device_id):
        with self._lock:
            self.store.remove_device(device_id)
            self._states.pop(device_id, None)
            self._ips.pop(device_id, None)
            self._next_poll.pop(device_id, None)
            self._wake.set()

    def refresh_once(self):
        with self._lock:
            devices = self.store.get_devices()
        for device in devices:
            # Release between devices so interactive operations can use the connection slot.
            with self._operation_lock:
                with self._lock:
                    if self._stop.is_set() or self._pending_operations:
                        return
                    if (not any(item["id"] == device["id"] for item in self.store.get_devices()) or
                            self.clock() < self._next_poll.get(device["id"], 0)):
                        continue
                try:
                    state = self._exchange(device)
                except StoreError:
                    state = "unknown"
                self._record(device["id"], state)

    def _run(self):
        while not self._stop.is_set():
            self._wake.clear()
            if self._stop.is_set():
                break
            self.refresh_once()
            with self._lock:
                devices = self.store.get_devices()
                due = [self._next_poll.get(device["id"], 0) for device in devices]
                delay = max(0, min(due) - self.clock()) if due and not self._pending_operations else None
            self._wake.wait(delay)

    def start(self):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name="devices", daemon=True)
                self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=0.2)
