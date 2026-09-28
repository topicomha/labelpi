#!/usr/bin/env python3
"""
Start labelpi.

    python run.py            # production: waitress on the configured host/port
    python run.py --dev      # development: Flask dev server with auto-reload
    LABELPI_MOCK=1 python run.py --dev   # no printers needed; PNGs go to ./out/

On the Pi, systemd runs this (deploy/labelpi.service); you normally don't.
"""

from __future__ import annotations

import argparse
import logging
import sys

from labelpi.app import create_app
from labelpi.config import ConfigError

# The waitress thread count: two printers can print at once, plus UI requests.
THREADS = 4


def main() -> int:
    parser = argparse.ArgumentParser(description="labelpi label printing service")
    parser.add_argument("--dev", action="store_true", help="Flask dev server with auto-reload")
    parser.add_argument("--config", help="path to printers.toml (default: config/printers.toml)")
    args = parser.parse_args()

    # Log to stdout; under systemd that ends up in the journal.
    logging.basicConfig(
        level=logging.DEBUG if args.dev else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    try:
        app = create_app(args.config)
    except (ConfigError, NotImplementedError) as exc:
        logging.error("%s", exc)
        return 1

    server = app.extensions["labelpi"].config.server
    if args.dev:
        app.run(host=server.host, port=server.port, debug=True)
    else:
        from waitress import serve

        logging.info("labelpi listening on %s:%s", server.host, server.port)
        serve(app, host=server.host, port=server.port, threads=THREADS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
