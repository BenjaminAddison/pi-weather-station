"""FastAPI routes for docs/pi-agent-api.md. Build with create_app()."""

import logging
import shutil
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, File, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException as StarletteHTTPException

from weatherpi import config as wconfig
from weatherpi import paths
from weatherpi.agent import AGENT_VERSION
from weatherpi.agent.auth import ClaimStore, UsbAddresses, classify_local_address
from weatherpi.agent.backup import Station
from weatherpi.agent.errors import ApiError, iso, utc_now
from weatherpi.agent.oscmd import Runner, default_runner
from weatherpi.agent.settings import SettingsManager
from weatherpi.agent.storage import StorageManager
from weatherpi.agent.system import SystemManager
from weatherpi.agent.tailscale import TailscaleManager
from weatherpi.agent.wifi import PRIORITY_MAX, PRIORITY_MIN, WifiManager

log = logging.getLogger("weatherpi.agent")

Via = Literal["usb", "tailscale"]


# ---------- request bodies (extra fields are ignored) ----------

class ClaimBody(BaseModel):
    password: str
    recovery_password: str
    ssh_pubkey: str
    api_token: str
    owner_name: str | None = None
    timezone: str | None = None


class RecoverBody(BaseModel):
    recovery_password: str
    new_password: str
    new_ssh_pubkey: str | None = None
    new_api_token: str | None = None


class PasswordBody(BaseModel):
    new_password: str


Priority = Annotated[StrictInt, Field(ge=PRIORITY_MIN, le=PRIORITY_MAX)]


class WifiConnectBody(BaseModel):
    ssid: str = Field(min_length=1)
    password: str = ""
    hidden: bool = False
    priority: Priority | None = None


class WifiSaveBody(BaseModel):
    ssid: str = Field(min_length=1)
    password: str | None = None
    hidden: bool = False
    priority: Priority = 0


class WifiPriorityBody(BaseModel):
    priority: Priority


class TailscaleUpBody(BaseModel):
    hostname: str = "weatherpi"


class TimeBody(BaseModel):
    utc: str


class MaintenanceBody(BaseModel):
    enabled: StrictBool


class SettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")  # an unknown setting is a mistake, not ignored
    record_interval_s: StrictInt


# ---------- the agent object (everything a route needs) ----------

class Agent:
    def __init__(self, runner: Runner, cfg: dict, via: Via | None, power_delay: float):
        self.runner = runner
        self.cfg = cfg
        self.via_override = via
        self.system = SystemManager(runner, cfg, power_delay=power_delay)
        self.wifi = WifiManager(runner, cfg)
        self.tailscale = TailscaleManager(runner, cfg)
        self.storage = StorageManager(
            runner, self.system.reboot,
            busy=lambda: self.wifi.job.running or self.tailscale.job.running)
        self.settings = SettingsManager(runner)
        self.claims = ClaimStore(runner, commit_etc=self.storage.commit_etc)
        self.usb_addresses = UsbAddresses(runner)
        self.station = Station(runner, cfg, self.system.hostname())

    def classify(self, request: Request) -> str | None:
        if self.via_override:
            return self.via_override
        server = request.scope.get("server") or (None, None)
        via = classify_local_address(server[0])
        if via is None and server[0]:
            via = classify_local_address(server[0], self.usb_addresses.get())
        return via


def get_agent(request: Request) -> Agent:
    return request.app.state.agent


def require_usb(request: Request) -> None:
    if request.state.via != "usb":
        raise ApiError(403, "usb_only", "This only works over the USB cable.")


def require_token(request: Request, agent: Annotated[Agent, Depends(get_agent)]) -> None:
    if not agent.claims.claimed:
        raise ApiError(409, "not_claimed", "This WeatherPi hasn't been set up yet.")
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not agent.claims.verify_token(token.strip()):
        raise ApiError(401, "unauthorized", "Missing or wrong API token.")


def create_app(
    runner: Runner | None = None,
    *,
    via: Via | Literal["auto"] | None = None,
    cfg: dict | None = None,
    power_delay: float | None = None,
) -> FastAPI:
    """Build the agent app.

    runner: command runner (default: FakeOS when WEATHERPI_FAKE=1, else RealRunner).
    via: force the arrival interface ("usb"/"tailscale"), or "auto" to classify by the
         request's local address. Default: "auto" in real mode, "usb" in fake mode (TestClient
         and localhost have no usb0/tailscale0 address).
    """
    runner = runner if runner is not None else default_runner()
    cfg = cfg if cfg is not None else wconfig.load_config()
    if via is None:
        via = "usb" if paths.FAKE else "auto"
    if power_delay is None:
        power_delay = 0.05 if paths.FAKE else 2.0
    agent = Agent(runner, cfg, None if via == "auto" else via, power_delay)

    app = FastAPI(title="weatherpi-agent", version=AGENT_VERSION, docs_url=None,
                  redoc_url=None, openapi_url="/v1/openapi.json" if paths.FAKE else None)
    app.state.agent = agent

    # ---------- errors ----------

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return JSONResponse(exc.body(), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        msg = f"{where}: {first.get('msg', 'invalid')}" if where else "invalid request"
        return JSONResponse({"error": "bad_request", "message": msg}, status_code=400)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "bad_request")
        return JSONResponse({"error": code, "message": str(exc.detail)},
                            status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def _internal(request: Request, exc: Exception):
        log.exception("unhandled error on %s", request.url.path)
        return JSONResponse({"error": "internal", "message": "internal error"}, status_code=500)

    # ---------- interface check on every request (belt and braces with nftables) ----------

    @app.middleware("http")
    async def _interface(request: Request, call_next):
        via_now = agent.classify(request)
        if via_now is None:
            return JSONResponse(
                {"error": "forbidden_interface",
                 "message": "The agent only answers on the USB cable and Tailscale."},
                status_code=403,
            )
        request.state.via = via_now
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    auth = [Depends(require_token)]
    usb = [Depends(require_usb)]

    # ---------- info and claim ----------

    @app.get("/v1/info")
    def info(request: Request):
        return {
            "agent_version": AGENT_VERSION,
            "image_version": paths.image_version(),
            "hostname": agent.system.hostname(),
            "model": agent.system.model(),
            "serial": agent.system.serial(),
            "claimed": agent.claims.claimed,
            "via": request.state.via,
            "time": iso(utc_now()),
            "clock_synced": agent.system.clock_synced(),
        }

    @app.post("/v1/claim", dependencies=usb)
    def claim(body: ClaimBody):
        if agent.claims.claimed:
            raise ApiError(409, "already_claimed", "This WeatherPi already has an owner.")
        return agent.claims.claim(
            body.password, body.recovery_password, body.ssh_pubkey, body.api_token,
            body.owner_name, body.timezone,
        )

    @app.post("/v1/recover", dependencies=usb)
    def recover(body: RecoverBody):
        return agent.claims.recover(
            body.recovery_password, body.new_password, body.new_ssh_pubkey, body.new_api_token
        )

    @app.post("/v1/password", dependencies=auth)
    def password(body: PasswordBody):
        agent.claims.set_password(body.new_password)
        return {"changed": True}

    # ---------- wifi ----------

    @app.get("/v1/wifi/scan", dependencies=auth)
    def wifi_scan():
        return agent.wifi.scan()

    @app.post("/v1/wifi/connect", dependencies=auth, status_code=202)
    def wifi_connect(body: WifiConnectBody):
        return agent.wifi.start_connect(body.ssid, body.password, body.hidden, body.priority)

    @app.get("/v1/wifi/status", dependencies=auth)
    def wifi_status():
        return agent.wifi.status()

    @app.get("/v1/wifi/saved", dependencies=auth)
    def wifi_saved():
        return agent.wifi.list_saved()

    @app.post("/v1/wifi/saved", dependencies=auth, status_code=201)
    def wifi_save(body: WifiSaveBody):
        return agent.wifi.save(body.ssid, body.password, body.hidden, body.priority)

    # {ssid:path}: an SSID may contain "/" (sent as %2F, which the server decodes)
    @app.patch("/v1/wifi/saved/{ssid:path}", dependencies=auth)
    def wifi_set_priority(ssid: str, body: WifiPriorityBody):
        return agent.wifi.set_priority(ssid, body.priority)

    @app.delete("/v1/wifi/saved/{ssid:path}", dependencies=auth)
    def wifi_delete(ssid: str):
        return agent.wifi.delete_saved(ssid)

    # ---------- tailscale ----------

    @app.get("/v1/tailscale/status", dependencies=auth)
    def ts_status():
        return agent.tailscale.status()

    @app.post("/v1/tailscale/up", dependencies=auth, status_code=202)
    def ts_up(body: TailscaleUpBody | None = None):
        hostname = (body.hostname if body else None) or agent.system.hostname()
        return agent.tailscale.start_up(hostname, internet=agent.wifi.internet())

    @app.post("/v1/tailscale/serve", dependencies=auth)
    def ts_serve():
        return agent.tailscale.serve()

    @app.post("/v1/tailscale/logout", dependencies=auth)
    def ts_logout():
        return agent.tailscale.logout()

    # ---------- station data ----------

    @app.get("/v1/sensors/live", dependencies=auth)
    def sensors_live():
        return agent.system.sensors_live(agent.station.read_live)

    @app.get("/v1/diagnostics", dependencies=auth)
    def diagnostics():
        out = agent.system.diagnostics(
            agent.wifi.status(), agent.tailscale.status(), agent.claims.secret_values()
        )
        out["storage"] = agent.storage.info()
        return out

    # ---------- backups ----------

    @app.get("/v1/backup", dependencies=auth)
    def backup():
        path, name = agent.station.create_backup()
        return FileResponse(
            path, media_type="application/gzip", filename=name,
            background=BackgroundTask(shutil.rmtree, path.parent, ignore_errors=True),
        )

    @app.post("/v1/restore", dependencies=auth)
    def restore(backup: Annotated[UploadFile, File()]):
        data = backup.file.read()
        return agent.station.restore(data)

    # ---------- system ----------

    @app.post("/v1/system/reboot", dependencies=auth, status_code=202)
    def reboot():
        return agent.system.reboot()

    @app.post("/v1/system/shutdown", dependencies=auth, status_code=202)
    def shutdown():
        return agent.system.shutdown()

    @app.post("/v1/system/time", dependencies=auth)
    def set_time(body: TimeBody):
        return agent.system.set_time(body.utc)

    @app.get("/v1/system/maintenance", dependencies=auth)
    def get_maintenance():
        return agent.storage.maintenance()

    @app.post("/v1/system/maintenance", dependencies=auth)
    def set_maintenance(body: MaintenanceBody):
        status, out = agent.storage.set_maintenance(body.enabled)
        return JSONResponse(out, status_code=status)

    # ---------- settings (D26) ----------

    @app.get("/v1/settings", dependencies=auth)
    def get_settings():
        return agent.settings.get()

    @app.put("/v1/settings", dependencies=auth)
    def put_settings(body: SettingsBody):
        return agent.settings.put(body.record_interval_s)

    return app
