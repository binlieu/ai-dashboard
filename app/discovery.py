"""Bounded, argument-list-only macOS ARP discovery."""

import ipaddress
import re
import subprocess
import time


def normalize_mac(text):
    if not isinstance(text, str) or not re.fullmatch(
            r"(?:[0-9a-fA-F]{1,2}:){5}[0-9a-fA-F]{1,2}|(?:[0-9a-fA-F]{1,2}-){5}[0-9a-fA-F]{1,2}", text):
        return None
    return ":".join("%02x" % int(part, 16) for part in text.replace("-", ":").split(":"))


def valid_ip(value):
    try:
        return str(ipaddress.IPv4Address(value)) if isinstance(value, str) else None
    except ipaddress.AddressValueError:
        return None


def parse_arp(text):
    entries = {}
    for line in text.splitlines():
        match = re.search(r"\(([^)]+)\)\s+at\s+(\S+)", line)
        if match:
            ip = valid_ip(match.group(1))
            mac = normalize_mac(match.group(2))
            if ip and mac:
                entries.setdefault(mac, ip)
    return entries


def ip_for_mac(mac, arp_text=None):
    normalized = normalize_mac(mac)
    if normalized is None:
        return None
    if arp_text is None:
        try:
            result = subprocess.run(["/usr/sbin/arp", "-an"], capture_output=True,
                                    text=True, timeout=2, check=False)
            arp_text = result.stdout if result.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return None
    return parse_arp(arp_text).get(normalized)


def sweep_local_network():
    deadline = time.monotonic() + 5
    children = []
    try:
        route = subprocess.run(["/sbin/route", "-n", "get", "default"], capture_output=True,
                               text=True, timeout=1, check=False)
        match = re.search(r"^\s*interface:\s*([a-zA-Z0-9]+)\s*$", route.stdout, re.M)
        if not match or route.returncode:
            return
        address = subprocess.run(["/usr/sbin/ipconfig", "getifaddr", match.group(1)],
                                 capture_output=True, text=True, timeout=1, check=False)
        ip = valid_ip(address.stdout.strip()) if address.returncode == 0 else None
        if not ip:
            return
        prefix = ip.rsplit(".", 1)[0]
        for host in range(1, 255):
            if time.monotonic() >= deadline:
                break
            children.append(subprocess.Popen(
                ["/sbin/ping", "-c", "1", "-t", "1", "%s.%s" % (prefix, host)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        for child in children:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                child.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                break
    except (OSError, subprocess.TimeoutExpired):
        pass
    finally:
        for child in children:
            if child.poll() is None:
                try:
                    child.kill()
                except OSError:
                    pass
        for child in children:
            try:
                child.wait(timeout=max(0, deadline + 0.25 - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass


def resolve_cameras(cameras, lookup=ip_for_mac, sweep=None):
    resolved = [dict(camera, ip=valid_ip(lookup(camera["mac"]))) for camera in cameras]
    if sweep is not None and any(camera["ip"] is None for camera in resolved):
        sweep()
        for camera in resolved:
            if camera["ip"] is None:
                camera["ip"] = valid_ip(lookup(camera["mac"]))
    return resolved
