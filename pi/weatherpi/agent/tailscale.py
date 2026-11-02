"""Tailscale via its CLI: `status --json`, `up`, `serve --bg`, `serve status --json`, `logout`."""

import json
import logging
import re

from weatherpi.agent.errors import ApiError, Job, JobFailed
from weatherpi.agent.oscmd import Runner

log = logging.getLogger("weatherpi.agent")

UP_TIMEOUT_S = 600  # how long `tailscale up` waits for the owner to sign in
SERVE_TIMEOUT_S = 20  # `serve` blocks while Serve is disabled on the tailnet; give up then
_SERVE_URL_RE = re.compile(r"https://login\.tailscale\.com/f/serve\S*")
_AUTH_URL_RE = re.compile(r"https://login\.tailscale\.com/a/\S+")
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def parse_status(raw: str) -> dict:
    """The subset of `tailscale status --json` the API reports."""
    try:
        data = json.loads(raw or "{}") or {}
    except ValueError:
        data = {}
    me = data.get("Self") or {}
    tailnet = data.get("CurrentTailnet") or {}
    ips = me.get("TailscaleIPs") or []
    ipv4 = next((ip for ip in ips if "." in ip), ips[0] if ips else None)
    dns = (me.get("DNSName") or "").rstrip(".") or None
    version = (data.get("Version") or "").split("-")[0] or None
    return {
        "version": version,
        "backend_state": data.get("BackendState") or "NoState",
        "auth_url": data.get("AuthURL") or None,
        "tailnet": tailnet.get("MagicDNSSuffix") or None,
        "tailnet_name": tailnet.get("Name") or None,
        "ip": ipv4,
        "dns_name": dns,
    }


def parse_serve_status(raw: str, port: int) -> tuple[bool, str | None]:
    """(serving our dashboard?, https URL) from `tailscale serve status --json`."""
    try:
        data = json.loads(raw or "{}") or {}
    except ValueError:
        return False, None
    target = f"127.0.0.1:{port}"
    for hostport, web in (data.get("Web") or {}).items():
        for handler in ((web or {}).get("Handlers") or {}).values():
            proxy = (handler or {}).get("Proxy") or ""
            if target in proxy or f"localhost:{port}" in proxy:
                host, _, p = hostport.rpartition(":")
                host = host or hostport
                url = f"https://{host}" if p in ("443", "") else f"https://{host}:{p}"
                return True, url
    return False, None


class TailscaleManager:
    def __init__(self, runner: Runner, cfg: dict):
        self.r = runner
        self.serve_port = int(cfg.get("tailscale", {}).get("serve_port", 8080))
        self.job = Job("tailscale-up")
        self.serve_error: str | None = None
        self.serve_admin_url: str | None = None
        self.last_error: str | None = None
        self._up_output_auth_url: str | None = None

    def status(self) -> dict:
        res = self.r.run(["tailscale", "status", "--json"], timeout=15)
        if res.missing:
            return {
                "installed": False, "version": None, "backend_state": "NoState",
                "auth_url": None, "tailnet": None, "tailnet_name": None, "ip": None,
                "dns_name": None, "serve_enabled": False, "serve_url": None,
                "serve_error": None, "last_error": "not_installed",
            }
        st = parse_status(res.stdout)
        error = self.last_error
        if not res.stdout.strip():
            error = error or "tailscaled_unreachable"
        if st["backend_state"] == "NeedsLogin" and not st["auth_url"] and self.job.running:
            st["auth_url"] = self._up_output_auth_url
        if st["backend_state"] != "NeedsLogin":
            st["auth_url"] = None
        serve_enabled, serve_url = False, None
        if st["backend_state"] == "Running":
            sres = self.r.run(["tailscale", "serve", "status", "--json"], timeout=15)
            if sres.ok:
                serve_enabled, serve_url = parse_serve_status(sres.stdout, self.serve_port)
        if serve_enabled:
            self.serve_error = self.serve_admin_url = None
        out = {
            "installed": True,
            **st,
            "serve_enabled": serve_enabled,
            "serve_url": serve_url,
            "serve_error": self.serve_error,
            "last_error": error,
            "up": self.job.as_dict(),
        }
        if self.serve_admin_url:
            out["admin_url"] = self.serve_admin_url
        return out

    def start_up(self, hostname: str, internet: bool) -> dict:
        if not _HOSTNAME_RE.match(hostname or ""):
            raise ApiError(400, "bad_request", "hostname must be a DNS label like 'weatherpi'")
        if not internet:
            raise ApiError(409, "no_internet", "Connect the WeatherPi to WiFi first.")
        self.job.start(lambda job: self._up(hostname), hostname=hostname)
        return {"job": self.job.name}

    def _up(self, hostname: str) -> None:
        self.last_error = None
        self._up_output_auth_url = None
        res = self.r.run(
            ["tailscale", "up", "--reset", f"--hostname={hostname}", "--ssh=false",
             f"--timeout={UP_TIMEOUT_S}s"],
            timeout=UP_TIMEOUT_S + 30,
        )
        m = _AUTH_URL_RE.search(res.output)
        if m:
            self._up_output_auth_url = m.group(0)
        if not res.ok:
            self.last_error = "timeout" if res.timed_out or "timeout" in res.output.lower() \
                else "up_failed"
            log.warning("tailscale up failed: %s", res.output[-300:])
            raise JobFailed(self.last_error)

    def serve(self) -> dict:
        st = parse_status(self.r.run(["tailscale", "status", "--json"], timeout=15).stdout)
        if st["backend_state"] != "Running":
            raise ApiError(409, "tailscale_not_running", "Sign the WeatherPi in to Tailscale first.")
        res = self.r.run(
            ["tailscale", "serve", "--bg", "--yes", f"http://127.0.0.1:{self.serve_port}"],
            timeout=SERVE_TIMEOUT_S,
        )
        text = res.output
        if "not enabled" in text.lower() or (res.timed_out and _SERVE_URL_RE.search(text)):
            m = _SERVE_URL_RE.search(text)
            self.serve_error = "serve_not_enabled_on_tailnet"
            self.serve_admin_url = m.group(0) if m else "https://login.tailscale.com/admin/settings/features"
            raise ApiError(
                409, "serve_not_enabled_on_tailnet",
                "Tailscale Serve is turned off for this tailnet. Open admin_url, enable it, "
                "then try again.",
                admin_url=self.serve_admin_url,
            )
        if not res.ok:
            self.serve_error = "serve_failed"
            log.warning("tailscale serve failed: %s", text[-300:])
            raise ApiError(500, "internal", "tailscale serve failed")
        self.serve_error = self.serve_admin_url = None
        return self.status()

    def logout(self) -> dict:
        if self.job.running:
            self.r.run(["tailscale", "down"], timeout=15)
        res = self.r.run(["tailscale", "logout"], timeout=30)
        if not res.ok:
            raise ApiError(500, "internal", "tailscale logout failed")
        self.serve_error = self.serve_admin_url = self.last_error = None
        return {"logged_out": True}
