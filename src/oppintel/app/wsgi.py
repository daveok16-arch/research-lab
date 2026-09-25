"""WSGI entry point and development server.

Production deployment uses `oppintel.app.wsgi:application` under a WSGI server. The module-level
`application` is what a WSGI server imports, so no application factory call is needed in the
server configuration.

    gunicorn --workers 2 --bind 0.0.0.0:8000 oppintel.app.wsgi:application

Two workers is deliberate for a SQLite-backed app: SQLite serializes writers, and more workers
mostly add lock contention rather than throughput. Reads are concurrent and dominate the
workload, so a small worker count with a thread pool is the better shape.
"""

from __future__ import annotations

import logging
import os

from .config import load_config
from .main import create_app

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)

#: The WSGI application object. A WSGI server imports this name directly.
application = create_app(load_config())


def main() -> None:
    """Run the development server.

    Not for production: the Flask development server is single-threaded, does not handle
    process management, and is explicitly not hardened. Use a WSGI server instead.
    """
    cfg = load_config()
    app = create_app(cfg)
    if not cfg.secret_key_from_env:
        print(
            "WARNING: SECRET_KEY is not set, so a random key was generated. Sessions will not "
            "survive a restart. Set SECRET_KEY before deploying."
        )
    app.run(host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "5000")),
            debug=cfg.debug)


if __name__ == "__main__":
    main()
