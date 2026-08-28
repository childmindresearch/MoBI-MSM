"""Entry point: `python -m app`.

The port is taken from the PORT environment variable when one is set, so a
launcher can assign it, and falls back to 8420 for a plain local run. Nothing
about this app needs a fixed port -- there are no OAuth callbacks, webhooks, or
cross-origin callers.
"""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "app.main:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8420")),
        log_level=os.environ.get("LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
