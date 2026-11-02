"""python3 -m weatherpi.web  ->  uvicorn on web.host:web.port from /etc/weatherpi/config.yaml."""

import uvicorn

from weatherpi.config import load_config
from weatherpi.web.app import create_app


def main() -> None:
    config = load_config()
    app = create_app(config)
    # No per-request access log: the page polls every refresh_interval and journald is on SD.
    uvicorn.run(
        app,
        host=str(config["web"]["host"]),
        port=int(config["web"]["port"]),
        access_log=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
