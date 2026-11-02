"""Shared helpers for agent tests (imported by conftest.py and the test modules)."""

import base64
import secrets

from fastapi.testclient import TestClient

PASSWORD = "owner-pass-123"
RECOVERY = "recovery-ABCDEFGHIJKLMNOPQRSTUVWX"


def make_token() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


def make_pubkey(comment: str = "weatherpi-companion@EXAMPLE-PC") -> str:
    blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + secrets.token_bytes(32)
    return f"ssh-ed25519 {base64.b64encode(blob).decode()} {comment}"


def claim_body(**over) -> dict:
    body = {
        "password": PASSWORD,
        "recovery_password": RECOVERY,
        "ssh_pubkey": make_pubkey(),
        "api_token": make_token(),
        "owner_name": "Example Owner",
        "timezone": "Africa/Johannesburg",
    }
    body.update(over)
    return body


def agent_of(client: TestClient):
    return client.app.state.agent
