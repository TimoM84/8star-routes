"""Run with `python -m app`. Uvicorn with a single worker; the scheduler runs in-process."""

import logging

import uvicorn

from .config import load_config
from .main import create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    app = create_app(cfg)
    uvicorn.run(app, host="0.0.0.0", port=cfg.port, proxy_headers=True,
                forwarded_allow_ips=cfg.trusted_proxies or "127.0.0.1", timeout_graceful_shutdown=30,
                access_log=False, log_level="info")


if __name__ == "__main__":
    main()
