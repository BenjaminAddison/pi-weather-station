"""Owner-changeable settings (D26): GET/PUT /v1/settings.

Stored in /var/lib/weatherpi/settings.yaml (on the data partition, so it survives the read-only
root) and merged over config.yaml by `weatherpi.config.load_config()`.
"""

import yaml

from weatherpi import config, paths
from weatherpi.agent.auth import write_private
from weatherpi.agent.errors import ApiError
from weatherpi.agent.oscmd import Runner

LOGGER_UNIT = "weatherpi-logger"


class SettingsManager:
    def __init__(self, runner: Runner):
        self.r = runner

    def get(self) -> dict:
        interval = int(config.load_config()["temp"]["write_interval"])
        return {
            "record_interval_s": interval,
            "record_interval_choices": list(config.RECORD_INTERVAL_CHOICES),
        }

    def put(self, record_interval_s: int) -> dict:
        if record_interval_s not in config.RECORD_INTERVAL_CHOICES:
            choices = ", ".join(str(c) for c in config.RECORD_INTERVAL_CHOICES)
            raise ApiError(400, "bad_request", f"record_interval_s must be one of {choices}")
        settings = config.load_settings() | {"record_interval_s": record_interval_s}
        paths.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        write_private(paths.SETTINGS_FILE, yaml.safe_dump(settings, sort_keys=True), 0o644)
        # the logger reads its intervals at start
        res = self.r.run(["systemctl", "restart", LOGGER_UNIT], timeout=60)
        if not res.ok and not res.missing:
            raise ApiError(500, "internal", "saved, but the logger didn't restart")
        return self.get()
