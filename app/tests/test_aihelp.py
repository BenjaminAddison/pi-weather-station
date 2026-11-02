from __future__ import annotations

from datetime import UTC, datetime

import pytest

from weatherpi_companion import aihelp
from weatherpi_companion.aihelp import REDACTED, redact, redact_obj, redact_text
from weatherpi_companion.checks import CheckResult
from weatherpi_companion.secrets import StationSecrets, generate_ssh_keypair

# ------------------------------------------------------------------------- text patterns


@pytest.mark.parametrize("text,secret", [
    ("password: hunter2hunter2", "hunter2hunter2"),
    ("password=hunter2hunter2", "hunter2hunter2"),
    ("PASSWORD = 'quoted secret'", "quoted secret"),
    ('new_password: "with spaces in it"', "with spaces in it"),
    ("recovery_password=abcde-fghjk-mnpqr-stuvw-xyz23", "abcde-fghjk-mnpqr-stuvw-xyz23"),
    ('{"password": "jsonsecret1"}', "jsonsecret1"),
    ('{"recovery_password":"jsonsecret2", "x": 1}', "jsonsecret2"),
    ('{"api_token": "tok_ABCdef123456"}', "tok_ABCdef123456"),
    ('"psk": "wifisecret99"', "wifisecret99"),
    ("psk=wifisecret99", "wifisecret99"),
    ('\tpsk="wpa supplicant secret"', "wpa supplicant secret"),
    ("802-11-wireless-security.psk:          mywifipass123", "mywifipass123"),
    ("wpa_passphrase=hostapdsecret", "hostapdsecret"),
    ("Authorization: Bearer abc.def.ghi-123456", "abc.def.ghi-123456"),
    ("curl -H 'Authorization: Bearer sekrit-token-123456'", "sekrit-token-123456"),
    ("bearer AbCdEfGhIjKlMnOp", "AbCdEfGhIjKlMnOp"),
    ("tailscale up --authkey=tskey-auth-kAbC123CNTRL-XyZ789", "kAbC123CNTRL-XyZ789"),
    ("key is tskey-api-k123456CNTRL-abcdef", "k123456CNTRL-abcdef"),
    ("TS_AUTHKEY=tskey-auth-xyz", "tskey-auth-xyz"),
    ("auth_key: something-secret", "something-secret"),
    ("To authenticate, visit: https://login.tailscale.com/a/1a2b3c4d5e", "1a2b3c4d5e"),
    ("https://user:pa55word@example.com/path", "pa55word"),
    ("?token=querysecret&x=1", "querysecret"),
    ("my wifi password is SuperSecret!", "SuperSecret!"),
    ("The password was: 'apple pie'", "apple pie"),
    ("client_secret = abc123secret", "abc123secret"),
    ("my recovery code abcde-fghjk-mnpqr-stuvw-xyz23 is lost", "abcde-fghjk-mnpqr-stuvw-xyz23"),
])
def test_redacts_patterns(text, secret):
    out = redact_text(text)
    assert secret not in out, out
    assert REDACTED in out or "PRIVATE KEY REDACTED" in out


@pytest.mark.parametrize("text,expected", [
    ("tailscale up --authkey=tskey-auth-kAbC123CNTRL-XyZ789",
     "tailscale up --authkey=tskey-[REDACTED]"),
    ("802-11-wireless-security.psk:          mywifipass123",
     "802-11-wireless-security.psk:          [REDACTED]"),
    ("auth_url: https://login.tailscale.com/a/abc123",
     "auth_url: https://login.tailscale.com/a/[REDACTED]"),
    ("token: abc  password: def", "token: [REDACTED]  password: [REDACTED]"),
    ("my wifi password is SuperSecret!", "my wifi password is [REDACTED]"),
    ('{"recovery_password":"jsonsecret2", "x": 1}', '{"recovery_password":"[REDACTED]", "x": 1}'),
    ("https://user:pa55word@example.com/path", "https://user:[REDACTED]@example.com/path"),
    ("Authorization: Bearer abc.def.ghi-123456", "Authorization: [REDACTED]"),
])
def test_redaction_exact_output(text, expected):
    assert redact_text(text) == expected
    assert redact_text(redact_text(text)) == expected  # idempotent


def test_private_keys_redacted():
    kp = generate_ssh_keypair("x")
    pem = kp.private_openssh.decode()
    out = redact_text(f"here is my key\n{pem}\nthanks")
    assert "OPENSSH PRIVATE KEY-----\n" not in out and "PRIVATE KEY REDACTED" in out
    assert "here is my key" in out and "thanks" in out
    body = pem.splitlines()[1]
    assert body not in out
    rsa = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"
    assert "MIIEpAIBAAKCAQEA" not in redact_text(rsa)
    # truncated key (no END line) is still removed
    assert "MIIEpAIB" not in redact_text("-----BEGIN PRIVATE KEY-----\nMIIEpAIB...")


def test_known_secrets_replaced_anywhere():
    s = StationSecrets.generate()
    text = f"token in log: {s.api_token} and again{s.api_token}; rec {s.recovery_password}"
    out = redact_text(text, s.known_values())
    assert s.api_token not in out and s.recovery_password not in out
    assert out.count(REDACTED) >= 3


def test_short_known_secrets_ignored():
    # avoid wiping every "a" in the text because someone passed a 1-char secret
    assert redact_text("a banana", ["a"]) == "a banana"


@pytest.mark.parametrize("text", [
    "PasswordAuthentication yes",
    "weatherpi-logger.service: active (running)",
    "Rain today: 0.84 mm (3 tips)",
    "dtoverlay=w1-gpio,gpiopin=9",
    "nodekey:0000aaaa1111",
    "backend_state: Running",
    "serve_url: https://weatherpi.example.ts.net",
    "Tailscale admin: https://login.tailscale.com/admin/machines",
    "pinctrl get 17",
    "rain pin is 17",
])
def test_harmless_text_kept(text):
    assert redact_text(text) == text


def test_emails_optional():
    t = "tailnet_name owner@example.com"
    assert redact_text(t) == t
    assert "owner@example.com" not in redact_text(t, hide_emails=True)


# ------------------------------------------------------------------------- structures


def test_redact_obj_by_key():
    obj = {"wifi": {"ssid": "ExampleWiFi", "psk": "x", "PSK": "y", "Password": "z"},
           "list": [{"api_token": "t1"}, {"token": "t2"}, {"tokens_used": 5}],
           "private_key": "k", "auth-key": "a", "empty_password": "", "none_psk": None,
           "secret_sauce": "s", "ok": "fine"}
    out = redact_obj(obj)
    assert out["wifi"] == {"ssid": "ExampleWiFi", "psk": REDACTED, "PSK": REDACTED,
                           "Password": REDACTED}
    assert out["list"][0]["api_token"] == REDACTED and out["list"][1]["token"] == REDACTED
    assert out["list"][2]["tokens_used"] == 5
    assert out["private_key"] == REDACTED and out["auth-key"] == REDACTED
    assert out["empty_password"] == "" and out["none_psk"] is None
    assert out["secret_sauce"] == REDACTED and out["ok"] == "fine"
    assert obj["wifi"]["psk"] == "x"  # input untouched


def test_redact_nested_strings_and_known():
    obj = {"journal": {"wpa": "psk=leaky123\nok line"}, "note": "tok ABCDEFGHIJKL"}
    out = redact(obj, ["ABCDEFGHIJKL"])
    assert "leaky123" not in out["journal"]["wpa"] and "ok line" in out["journal"]["wpa"]
    assert "ABCDEFGHIJKL" not in out["note"]
    assert redact("password=abc12345") == f"password={REDACTED}"


# ------------------------------------------------------------------------- the pack


DIAG = {
    "collected_at": "2026-10-19T11:05:00Z",
    "system": {"uptime_s": 1234, "throttled": "0x50000"},
    "services": [{"name": "weatherpi-logger", "active": "failed"}],
    "journal": {"weatherpi-logger": "line1\nERROR sensor\nAuthorization: Bearer leakedtoken123",
                "NetworkManager": "802-11-wireless-security.psk: leakedpsk"},
    "network": {"wifi": {"ssid": "ExampleWiFi", "psk": "jsonleak"},
                "tailscale": {"auth_url": "https://login.tailscale.com/a/leakurl"}},
    "config": {"station": {"name": "WeatherPi"}},
}


def test_build_pack_contains_sections_and_is_redacted():
    s = StationSecrets.generate()
    checks = [CheckResult(True, "driver_ok", "Driver installed."),
              CheckResult(False, "host_tailscale_off", "Tailscale is off.", "Stopped")]
    text = aihelp.build_help_pack(
        owner_description=f"No data since Tuesday. My password is hunter2hunter2. {s.api_token}",
        diagnostics=DIAG, host_checks=checks,
        station={"serial": "00000000abcd1234", "hostname": "weatherpi",
                 "tailnet": "example.ts.net"},
        app_log_tail="12:00 connecting with token " + s.api_token,
        known_secrets=s.known_values(),
        now=datetime(2026, 10, 19, 12, 0, tzinfo=UTC),
    )
    # base text present and unmodified
    assert text.startswith("# WeatherPi help pack")
    assert "BCM GPIO 9 (physical pin 21)" in text and "BCM GPIO 17 (physical pin 11)" in text
    assert "0.2794 mm" in text
    assert "Never ask for passwords" in text
    # owner + diagnostics + host checks + log
    assert "No data since Tuesday" in text
    assert "collected 2026-10-19 12:00 UTC" in text
    assert "- PASS `driver_ok`: Driver installed." in text
    assert "- FAIL `host_tailscale_off`: Tailscale is off. (Stopped)" in text
    assert "#### Journal: weatherpi-logger" in text and "ERROR sensor" in text
    assert '"throttled": "0x50000"' in text
    assert "Recent Companion app log" in text
    # nothing secret survives
    for leak in ("hunter2hunter2", "leakedtoken123", "leakedpsk", "jsonleak", "leakurl",
                 s.api_token, s.recovery_password):
        assert leak not in text, leak


def test_build_pack_when_offline_and_emails_hidden():
    text = aihelp.build_help_pack(owner_description="", diagnostics=None,
                                  diagnostics_error="not reachable over Tailscale or USB",
                                  station={"tailnet_name": "owner@example.com"},
                                  base_text="# Base")
    assert text.startswith("# Base\n\n---")
    assert "didn't describe the problem" in text
    assert "couldn't reach the WeatherPi: not reachable over Tailscale or USB" in text
    assert "No host checks were run" in text
    assert "owner@example.com" not in text


def test_base_instructions_resource():
    base = aihelp.base_instructions()
    for needle in ("DS18B20", "BCM GPIO 9", "physical pin 21", "BCM GPIO 17", "physical pin 11",
                   "0.2794", "weatherpi-logger", "weatherpi-agent", "tailscale serve",
                   "paste the output", "Never ask for passwords", "Safe vs. unsafe"):
        assert needle in base, needle


def test_save_pack(tmp_path):
    p = aihelp.save_pack("hello", tmp_path, now=datetime(2026, 10, 19, 12, 0, 1, tzinfo=UTC))
    assert p.name == "weatherpi-ai-help-20261019-120001.md" and p.read_text() == "hello"
