"""Firewall (nftables), sshd drop-in precedence, and journald drop-in."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from systest_helpers import PI_DIR, parse_unit

NFT = PI_DIR / "nftables.conf"
NFT_STUB = PI_DIR / "etc/nftables.conf"
SSHD_DIR = PI_DIR / "etc/ssh/sshd_config.d"
# The file the agent (W2) writes at claim to turn on password login (D8).
AGENT_SSHD_PASSWORD_FILE = "10-weatherpi-password.conf"


def code(path: Path) -> str:
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].rstrip()
        if line.strip():
            lines.append(line)
    return "\n".join(lines)


def chain(name: str) -> str:
    m = re.search(rf"chain {name} \{{(.*?)\n    \}}", code(NFT), re.DOTALL)
    assert m, f"chain {name} not found"
    return m.group(1)


def ports(text: str, proto: str) -> set[int]:
    found: set[int] = set()
    for m in re.finditer(rf"{proto} dport (\{{[^}}]*\}}|\d+) accept", text):
        found |= {int(p) for p in re.findall(r"\d+", m.group(1))}
    return found


# --- nftables ------------------------------------------------------------------------


def test_only_manages_own_table():
    text = code(NFT)
    assert "flush ruleset" not in text
    assert re.findall(r"^table (\S+ \S+)", text, re.MULTILINE) == ["inet weatherpi"] * 2
    assert "delete table inet weatherpi" in text
    assert text.count("{") == text.count("}")


def test_input_policy_and_basics():
    c = chain("input")
    assert "type filter hook input priority filter; policy drop;" in c
    assert 'iif "lo" accept' in c
    assert "ct state established,related accept" in c
    assert "icmp" in c and "ipv6-icmp" in c
    assert "udp sport 67 udp dport 68 accept" in c
    assert ports(c, "udp") >= {41641, 5353}
    assert 'iifname "usb0" jump usb_in' in c
    assert 'iifname "tailscale0" jump tailnet_in' in c
    assert ports(c, "tcp") == set(), "no TCP service may be open on every interface"


def test_usb_and_tailnet_services():
    assert ports(chain("usb_in"), "tcp") == {22, 8765, 53}
    assert ports(chain("usb_in"), "udp") == {67, 53}
    assert ports(chain("tailnet_in"), "tcp") == {22, 80, 443, 8765}
    assert ports(chain("tailnet_in"), "udp") == set()


def test_wlan0_has_no_inbound_services():
    assert "wlan0" not in code(NFT), "D23: wlan0 is uplink only"
    assert "wlan0" in NFT.read_text(), "keep the D23 comment explaining why"


def test_forward_accepts_for_nm_shared_mode():
    assert "type filter hook forward priority filter; policy accept;" in chain("forward")


def test_stub_includes_dropin_dir_without_flushing():
    text = code(NFT_STUB)
    assert NFT_STUB.read_text().startswith("#!/usr/sbin/nft -f\n")
    assert 'include "/etc/nftables.d/*.nft"' in text
    assert "flush" not in text
    assert "WeatherPi" in NFT_STUB.read_text(), "install.sh detects its own stub by this marker"


@pytest.mark.skipif(not (shutil.which("nft") and shutil.which("unshare")), reason="needs nft")
def test_nft_check_syntax():
    result = subprocess.run(
        ["unshare", "-rn", "nft", "-c", "-f", str(NFT)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 and "Operation not permitted" in result.stderr:
        pytest.skip("no user namespaces")
    assert result.returncode == 0, result.stderr


# --- sshd ----------------------------------------------------------------------------


def effective_sshd(files: dict[str, str]) -> dict[str, str]:
    """sshd semantics: drop-ins read in lexical order, FIRST value per keyword wins."""
    out: dict[str, str] = {}
    for name in sorted(files):
        for line in files[name].splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            key, _, value = line.partition(" ")
            out.setdefault(key.lower(), value.strip())
    return out


def shipped_sshd() -> dict[str, str]:
    return {p.name: p.read_text() for p in SSHD_DIR.glob("*.conf")}


def test_sshd_before_claim_is_key_only():
    eff = effective_sshd(shipped_sshd())
    assert eff["passwordauthentication"] == "no"
    assert eff["kbdinteractiveauthentication"] == "no"
    assert eff["permitrootlogin"] == "no"
    assert eff["pubkeyauthentication"] == "yes"


def test_sshd_cloud_init_dropin_cannot_enable_passwords():
    files = shipped_sshd() | {"50-cloud-init.conf": "PasswordAuthentication yes\n"}
    assert effective_sshd(files)["passwordauthentication"] == "no"


def test_sshd_agent_claim_file_wins():
    files = shipped_sshd() | {AGENT_SSHD_PASSWORD_FILE: "PasswordAuthentication yes\n"}
    eff = effective_sshd(files)
    assert eff["passwordauthentication"] == "yes"
    assert eff["permitrootlogin"] == "no"
    assert AGENT_SSHD_PASSWORD_FILE < min(shipped_sshd())
    # The mechanism is documented where an admin will look.
    assert AGENT_SSHD_PASSWORD_FILE in (SSHD_DIR / "40-weatherpi.conf").read_text()


# --- journald ------------------------------------------------------------------------


def test_journald_cap():
    s = parse_unit(PI_DIR / "etc/systemd/journald.conf.d/weatherpi.conf")
    assert s["Journal"]["SystemMaxUse"] == ["16M"]
