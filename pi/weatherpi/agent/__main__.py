"""`python3 -m weatherpi.agent` -> uvicorn on 0.0.0.0:<agent.port> (systemd: weatherpi-agent)."""

import argparse
import logging

import uvicorn

from weatherpi import config
from weatherpi.agent.app import create_app


def main() -> None:
    cfg = config.load_config()
    parser = argparse.ArgumentParser(prog="python3 -m weatherpi.agent")
    parser.add_argument("--host", default="0.0.0.0",
                        help="bind address (nftables limits it to usb0/tailscale0)")
    parser.add_argument("--port", type=int, default=int(cfg["agent"]["port"]))
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(levelname)s %(name)s: %(message)s")
    uvicorn.run(
        create_app(cfg=cfg),
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        proxy_headers=False,
        server_header=False,
        access_log=False,
    )


if __name__ == "__main__":
    main()
