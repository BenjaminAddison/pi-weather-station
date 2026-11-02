"""Unit tests for the parsers and helpers (no HTTP)."""

import json

import pytest

from weatherpi.agent import auth
from weatherpi.agent.oscmd import RealRunner
from weatherpi.agent.tailscale import parse_serve_status, parse_status
from weatherpi.agent.wifi import classify_failure, normalize_security, split_terse


def test_split_terse():
    assert split_terse(r"*:My\:Net:72:WPA2") == ["*", "My:Net", "72", "WPA2"]
    assert split_terse(r" :back\\slash:5:") == [" ", "back\\slash", "5", ""]


@pytest.mark.parametrize(("raw", "out"), [
    ("", "open"), ("--", "open"), ("WPA2", "WPA2"), ("WPA1 WPA2", "WPA2"), ("WPA3", "WPA3"),
    ("WPA2 WPA3", "WPA2/WPA3"), ("WPA2 802.1X", "enterprise"), ("WEP", "WEP"),
])
def test_normalize_security(raw, out):
    assert normalize_security(raw) == out


@pytest.mark.parametrize(("text", "timed_out", "code"), [
    ("Error: Connection activation failed: Secrets were required, but not provided.", False,
     "wrong_password"),
    ("Error: No network with SSID 'x' found.", False, "not_found"),
    ("Error: Connection activation failed: The Wi-Fi network could not be found", False,
     "not_found"),
    ("Error: Timeout expired (45 seconds)", False, "timeout"),
    ("", True, "timeout"),
    ("Error: something else", False, "failed"),
])
def test_classify_failure(text, timed_out, code):
    assert classify_failure(text, timed_out) == code


def test_parse_tailscale_status_running():
    raw = json.dumps({
        "Version": "1.94.2-t0123abcd-g4567",
        "BackendState": "Running",
        "AuthURL": "",
        "Self": {"DNSName": "weatherpi.example.ts.net.",
                 "TailscaleIPs": ["fd7a:115c:a1e0::1", "100.94.2.31"]},
        "CurrentTailnet": {"Name": "owner@example.com", "MagicDNSSuffix": "example.ts.net"},
    })
    st = parse_status(raw)
    assert st == {"version": "1.94.2", "backend_state": "Running", "auth_url": None,
                  "tailnet": "example.ts.net", "tailnet_name": "owner@example.com",
                  "ip": "100.94.2.31", "dns_name": "weatherpi.example.ts.net"}


def test_parse_tailscale_status_garbage():
    st = parse_status("not json")
    assert st["backend_state"] == "NoState" and st["ip"] is None


def test_parse_serve_status():
    raw = json.dumps({"TCP": {"443": {"HTTPS": True}}, "Web": {
        "weatherpi.example.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8080"}}}}})
    assert parse_serve_status(raw, 8080) == (True, "https://weatherpi.example.ts.net")
    assert parse_serve_status(raw, 9999) == (False, None)
    assert parse_serve_status("{}", 8080) == (False, None)
    assert parse_serve_status("", 8080) == (False, None)


@pytest.mark.parametrize(("ip", "usb", "via"), [
    ("10.12.194.1", set(), "usb"),
    ("::ffff:10.12.194.1", set(), "usb"),
    ("192.168.137.50", {"192.168.137.50"}, "usb"),  # usb0 as an ICS DHCP client
    ("100.64.0.1", set(), "tailscale"),
    ("100.127.255.254", set(), "tailscale"),
    ("fd7a:115c:a1e0::5e01:21f", set(), "tailscale"),
    ("100.128.0.1", set(), None),
    ("192.168.7.9", set(), None),
    ("127.0.0.1", set(), None),
    ("testserver", set(), None),
    (None, set(), None),
])
def test_classify_local_address(ip, usb, via):
    assert auth.classify_local_address(ip, usb) == via


def test_scrypt_roundtrip():
    rec = auth.scrypt_hash("recovery-ABCDEFGHIJKLMNOPQRSTUVWX")
    assert rec["n"] == 2**14 and rec["r"] == 8 and rec["p"] == 1
    assert auth.scrypt_verify("recovery-ABCDEFGHIJKLMNOPQRSTUVWX", rec)
    assert not auth.scrypt_verify("recovery-ABCDEFGHIJKLMNOPQRSTUVWY", rec)
    assert not auth.scrypt_verify("x", {})


def test_real_runner_missing_command():
    res = RealRunner().run(["weatherpi-no-such-command-xyz"], timeout=5)
    assert res.missing and not res.ok


def test_real_runner_input_and_timeout():
    import sys

    echo = RealRunner().run(
        [sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"], input="main:x\n"
    )
    assert echo.ok and echo.stdout.strip() == "MAIN:X"
    slow = RealRunner().run(
        [sys.executable, "-c", "import time; print('partial', flush=True); time.sleep(10)"],
        timeout=1,
    )
    assert slow.timed_out and not slow.ok
