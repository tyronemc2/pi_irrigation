"""Entry point: python -m irrigation --config /etc/pi_irrigation/config.yaml"""
from __future__ import annotations

import argparse
import logging
import signal
import sys

from .app import create_app
from .config import ConfigError, load_config
from .system import IrrigationSystem


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Greenhouse irrigation controller")
    p.add_argument("--config", default="/etc/pi_irrigation/config.yaml")
    p.add_argument("--simulate", action="store_true", help="use a simulated ESP32")
    p.add_argument("--state", help="override state file path")
    p.add_argument("--port", type=int)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = load_config(args.config) if args.config != "-" else load_config(data={})
    except ConfigError as exc:
        if args.simulate:
            logging.warning("%s - using defaults", exc)
            cfg = load_config(data={})
        else:
            logging.error("%s", exc)
            return 2
    if args.simulate:
        cfg["controller"]["mode"] = "simulated"
    if args.state:
        cfg["state_file"] = args.state
    if args.port:
        cfg["web"]["port"] = args.port

    system = IrrigationSystem(cfg)
    system.start()

    def _shutdown(*_):
        logging.info("Shutting down: closing all valves")
        system.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    app = create_app(system)
    host, port = cfg["web"]["host"], cfg["web"]["port"]
    try:
        from waitress import serve
        logging.info("Dashboard on http://%s:%s", host, port)
        serve(app, host=host, port=port, threads=4)
    except ImportError:
        app.run(host=host, port=port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
