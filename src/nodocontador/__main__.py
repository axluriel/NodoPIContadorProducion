from __future__ import annotations

import logging
import os

from aiohttp import web

from .app import create_app


def main() -> None:
    logging.basicConfig(
        level=os.getenv("CONTADOR_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    web.run_app(
        create_app(),
        host=os.getenv("CONTADOR_HOST", "127.0.0.1"),
        port=int(os.getenv("CONTADOR_PORT", "5000")),
        access_log_format='%a "%r" %s %b %Tf',
    )


if __name__ == "__main__":
    main()
