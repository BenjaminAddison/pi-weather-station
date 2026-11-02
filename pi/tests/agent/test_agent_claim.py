import json
import time

import pytest
from agent_helpers import PASSWORD, RECOVERY, agent_of, claim_body, make_pubkey, make_token

from weatherpi import paths
from weatherpi.agent import auth


def test_info_unclaimed(client):
    r = client.get("/v1/info")
    assert r.status_code == 200
    info = r.json()
    assert info["claimed"] is False
    assert info["via"] == "usb"
    assert info["agent_version"].startswith("2.")
    assert info["time"].endswith("Z")
    assert set(info) >= {"image_version", "hostname", "model", "serial", "clock_synced"}


def test_info_via_tailscale(make_client):
    assert make_client(via="tailscale").get("/v1/info").json()["via"] == "tailscale"


@pytest.mark.parametrize(
    ("base_url", "status", "via"),
    [
        ("http://10.12.194.1:8765", 200, "usb"),
        ("http://100.94.2.31:8765", 200, "tailscale"),
        ("http://192.168.7.9:8765", 403, None),
        ("http://127.0.0.1:8765", 403, None),
    ],
)
def test_interface_classification_by_local_address(make_client, fake, base_url, status, via):
    c = make_client(via="auto", base_url=base_url)
    r = c.get("/v1/info")
    assert r.status_code == status
    if via:
        assert r.json()["via"] == via
    else:
        assert r.json()["error"] == "forbidden_interface"


def test_claim_happy_path(client, fake, root):
    body = claim_body()
    r = client.post("/v1/claim", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["claimed"] is True
    assert client.get("/v1/info").json()["claimed"] is True

    assert fake.passwords["main"] == PASSWORD
    assert fake.timezone == "Africa/Johannesburg"
    keys = (paths.MAIN_HOME / ".ssh/authorized_keys").read_text()
    assert keys.strip() == body["ssh_pubkey"]
    state = paths.AGENT_STATE_DIR
    assert (state / "claimed").exists()
    assert body["api_token"] not in (state / "token.sha256").read_text()
    assert (state / "token.sha256").read_text().strip() == auth.token_digest(body["api_token"])
    rec = json.loads((state / "recovery.hash").read_text())
    assert (rec["alg"], rec["n"], rec["r"], rec["p"]) == ("scrypt", 2**14, 8, 1)
    assert RECOVERY not in (state / "recovery.hash").read_text()
    conf = (root / "etc/ssh/sshd_config.d/10-weatherpi-password.conf").read_text()
    assert conf == "PasswordAuthentication yes\n"
    assert ["systemctl", "reload", "ssh"] in fake.calls


def test_claim_default_timezone_utc(client, fake):
    fake.timezone = "Europe/Example"
    assert client.post("/v1/claim", json=claim_body(timezone=None)).status_code == 200
    assert fake.timezone == "UTC"


def test_claim_usb_only(make_client):
    r = make_client(via="tailscale").post("/v1/claim", json=claim_body())
    assert r.status_code == 403
    assert r.json()["error"] == "usb_only"


def test_claim_only_once(claimed):
    client, _, _ = claimed
    r = client.post("/v1/claim", json=claim_body())
    assert r.status_code == 409
    assert r.json()["error"] == "already_claimed"


@pytest.mark.parametrize("pw", ["short", "", "has\nnewline-root:x", "x" * 129])
def test_claim_weak_password(client, fake, pw):
    r = client.post("/v1/claim", json=claim_body(password=pw))
    assert r.status_code == 400
    assert r.json()["error"] == "weak_password"
    assert not fake.passwords
    assert client.get("/v1/info").json()["claimed"] is False


@pytest.mark.parametrize(
    "over",
    [
        {"recovery_password": "too-short"},
        {"api_token": "not a token!"},
        {"ssh_pubkey": "ssh-ed25519"},
        {"ssh_pubkey": "ssh-dss AAAAB3NzaC1kc3MAAACBAP= x"},
        {"ssh_pubkey": make_pubkey() + "\n" + make_pubkey()},
        {"timezone": "../../etc/passwd"},
    ],
)
def test_claim_bad_request(client, over):
    r = client.post("/v1/claim", json=claim_body(**over))
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request"


def test_claim_missing_field(client):
    body = claim_body()
    del body["api_token"]
    r = client.post("/v1/claim", json=body)
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request"
    assert "api_token" in r.json()["message"]


def test_protected_endpoint_states(client):
    r = client.get("/v1/wifi/status")
    assert r.status_code == 409 and r.json()["error"] == "not_claimed"
    body = claim_body()
    client.post("/v1/claim", json=body)
    assert client.get("/v1/wifi/status").json()["error"] == "unauthorized"
    bad = {"Authorization": "Bearer " + make_token()}
    r = client.get("/v1/wifi/status", headers=bad)
    assert r.status_code == 401 and r.json()["error"] == "unauthorized"
    r = client.get("/v1/wifi/status", headers={"Authorization": body["api_token"]})
    assert r.status_code == 401
    good = {"Authorization": "Bearer " + body["api_token"]}
    assert client.get("/v1/wifi/status", headers=good).status_code == 200


def test_password_change(claimed, fake):
    client, h, _ = claimed
    r = client.post("/v1/password", headers=h, json={"new_password": "brand-new-pass"})
    assert r.status_code == 200
    assert fake.passwords["main"] == "brand-new-pass"
    r = client.post("/v1/password", headers=h, json={"new_password": "short"})
    assert r.status_code == 400 and r.json()["error"] == "weak_password"
    assert client.post("/v1/password", json={"new_password": "brand-new-pass"}).status_code == 401


def test_password_change_works_over_tailscale(claimed, make_client, fake):
    _, h, _ = claimed
    c = make_client(via="tailscale")
    assert c.post("/v1/password", headers=h, json={"new_password": "via-tailnet-1"}).status_code \
        == 200


def test_recover_rekeys(claimed, fake):
    client, old_h, body = claimed
    keys_file = paths.MAIN_HOME / ".ssh/authorized_keys"
    # the owner added a key of their own by hand; recovery must keep it
    own_key = make_pubkey("owner@laptop")
    keys_file.write_text(keys_file.read_text() + own_key + "\n")
    new_key, new_token = make_pubkey("weatherpi-companion@NEW-PC"), make_token()
    r = client.post("/v1/recover", json={
        "recovery_password": RECOVERY, "new_password": "after-recovery-1",
        "new_ssh_pubkey": new_key, "new_api_token": new_token,
    })
    assert r.status_code == 200, r.text
    assert r.json()["recovered"] is True
    assert fake.passwords["main"] == "after-recovery-1"
    keys = keys_file.read_text()
    assert new_key in keys and own_key in keys and body["ssh_pubkey"] not in keys
    assert client.get("/v1/wifi/status", headers=old_h).status_code == 401
    new_h = {"Authorization": f"Bearer {new_token}"}
    assert client.get("/v1/wifi/status", headers=new_h).status_code == 200


def test_recover_password_only_keeps_token(claimed, fake):
    client, h, _ = claimed
    r = client.post("/v1/recover", json={"recovery_password": RECOVERY,
                                         "new_password": "only-the-password"})
    assert r.status_code == 200
    assert client.get("/v1/wifi/status", headers=h).status_code == 200


def test_recover_usb_only_and_needs_claim(client, make_client):
    body = {"recovery_password": RECOVERY, "new_password": "whatever-123"}
    r = client.post("/v1/recover", json=body)
    assert r.status_code == 409 and r.json()["error"] == "not_claimed"
    client.post("/v1/claim", json=claim_body())
    r = make_client(via="tailscale").post("/v1/recover", json=body)
    assert r.status_code == 403 and r.json()["error"] == "usb_only"


def test_recover_bad_then_lockout(claimed, fake, make_client):
    client, _, _ = claimed
    bad = {"recovery_password": "wrong-recovery-password-000", "new_password": "new-pass-123"}
    for i in range(auth.MAX_RECOVER_FAILURES):
        r = client.post("/v1/recover", json=bad)
        assert r.status_code == 403
        assert r.json()["error"] == "bad_recovery"
        assert r.json()["attempts_left"] == auth.MAX_RECOVER_FAILURES - i - 1
    assert r.json()["locked"] is True
    good = {"recovery_password": RECOVERY, "new_password": "new-pass-123"}
    r = client.post("/v1/recover", json=good)
    assert r.status_code == 403 and r.json()["locked"] is True
    assert r.json()["retry_after_s"] > 500
    assert "main" in fake.passwords and fake.passwords["main"] == PASSWORD

    # persisted: a restarted agent is still locked
    r = make_client().post("/v1/recover", json=good)
    assert r.status_code == 403 and r.json()["locked"] is True

    # after the lockout expires the right password works again
    lock_file = paths.AGENT_STATE_DIR / "recover.lockout"
    data = json.loads(lock_file.read_text())
    data["locked_until"] = time.time() - 1
    lock_file.write_text(json.dumps(data))
    r = client.post("/v1/recover", json=good)
    assert r.status_code == 200, r.text
    assert not lock_file.exists()


def test_recover_weak_new_password(claimed):
    client, _, _ = claimed
    r = client.post("/v1/recover", json={"recovery_password": RECOVERY, "new_password": "x"})
    assert r.status_code == 400 and r.json()["error"] == "weak_password"


def test_claim_concurrent_is_busy(client, fake):
    store = agent_of(client).claims
    assert store._lock.acquire()
    try:
        r = client.post("/v1/claim", json=claim_body())
        assert r.status_code == 409 and r.json()["error"] == "busy"
    finally:
        store._lock.release()
