"""Bounded, unicast-only Kasa discovery that retains only kind and model."""

import ipaddress
import json
import math
import re
import socket
import time

LEGACY_PORT = 9999
NEW_PORT = 20002
# Autokey XOR of {"system":{"get_sysinfo":{}}}, starting with key 171.
LEGACY_QUERY = bytes.fromhex("d0f281f88bff9af7d5ef94b6d1b4c09fec95e68fe187e8caf08bf68bf6")
NEW_QUERY = bytes.fromhex("020000010000000000000000463cb5d3")


def _clean_model(value):
    if not isinstance(value, str):
        return None
    value = re.sub(r"\([A-Za-z]{2}\)$", "", value)
    return value if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.+-]{0,23}", value) else None


def _kind(model, device_type):
    for prefixes, kind in ((("HS1", "HS3", "KP", "EP"), "plug"),
                           (("HS2", "KS", "ES"), "switch"),
                           (("KL", "LB"), "light"), (("KC", "EC", "KD"), "camera")):
        if model and model.upper().startswith(prefixes):
            return kind
    device_type = device_type.upper() if isinstance(device_type, str) else ""
    for names, kind in ((("CAMERA",), "camera"), (("PLUGSWITCH",), "plug_or_switch"),
                        (("SWITCH",), "switch"), (("PLUG",), "plug"),
                        (("BULB", "LIGHT"), "light"), (("HOMEWIFISYSTEM", "ROUTER"), "router")):
        if any(name in device_type for name in names):
            return kind
    return "other"


def parse_identity(port, data):
    """Discard malformed answers and every field except a safe model and kind."""
    try:
        if not isinstance(port, int) or not isinstance(data, (bytes, bytearray)) or len(data) > 8192:
            return None
        if port == LEGACY_PORT:
            key, plain = 171, bytearray()
            for byte in data:
                plain.append(key ^ byte)
                key = byte
            payload = json.loads(plain.decode("utf-8"))
            details = payload["system"]["get_sysinfo"]
            if not isinstance(details, dict):
                return None
            if isinstance(details.get("system"), dict):
                details = details["system"]
            model = _clean_model(details.get("model"))
            device_type = details.get("mic_type")
            if not isinstance(device_type, str):
                device_type = details.get("type")
        elif port == NEW_PORT:
            payload = json.loads(data[16:].decode("utf-8"))
            details = payload["result"]
            if not isinstance(details, dict):
                return None
            model = _clean_model(details.get("device_model"))
            device_type = details.get("device_type")
        else:
            return None
        if model is None and not isinstance(device_type, str):
            return None
        return {"kind": _kind(model, device_type), "model": model}
    except Exception:
        # Device-controlled datagrams and arbitrary input types must never escape the parser.
        return None


def describe(identity):
    if identity is None:
        return "The address did not answer as a Kasa camera."
    model = " (%s)" % identity["model"] if identity["model"] else ""
    if identity["kind"] == "camera":
        return ("This looks like a camera%s, but it did not accept a video connection. "
                "Check that it is online and try again.") % model
    names = {"plug": "Kasa smart plug", "switch": "Kasa smart switch",
             "plug_or_switch": "Kasa smart plug or switch", "light": "Kasa smart light",
             "router": "TP-Link Wi-Fi router", "other": "TP-Link device"}
    return "This is a %s%s, not a camera." % (names[identity["kind"]], model)


def identify_device(ip, timeout=1.5, socket_factory=None):
    """Ask one IPv4 device twice at most, sharing a single total time budget."""
    connection = None
    try:
        if not isinstance(ip, str):
            return None
        address = ipaddress.IPv4Address(ip)
        if address.is_multicast or address.is_unspecified or int(address) == 0xffffffff:
            return None
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            return None
        started = time.monotonic()
        deadline = started + timeout
        connection = socket_factory() if socket_factory is not None else socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for phase_deadline in (started + timeout / 2, deadline):
            for port, query in ((LEGACY_PORT, LEGACY_QUERY), (NEW_PORT, NEW_QUERY)):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                connection.settimeout(remaining)
                connection.sendto(query, (ip, port))
            while True:
                remaining = phase_deadline - time.monotonic()
                if remaining <= 0:
                    break
                connection.settimeout(remaining)
                try:
                    # One extra byte lets the parser reject oversized datagrams, even if truncated.
                    data, source = connection.recvfrom(8193)
                except (socket.timeout, TimeoutError):
                    break
                if time.monotonic() >= deadline:
                    return None
                if source not in ((ip, LEGACY_PORT), (ip, NEW_PORT)):
                    continue
                identity = parse_identity(source[1], data)
                if identity is not None:
                    return identity
    except OSError:
        return None
    except Exception:
        return None
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
    return None
