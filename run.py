from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn


def env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)

    if value is None:
        return default

    return value.strip().lower() in {"1", "true", "yes", "on"}


if __name__ == "__main__":
    env_file = ".env" if Path(".env").is_file() else None

    uvicorn.run(
        "app.main:app",
        host=os.getenv("SCRAPER_HOST", "0.0.0.0"),
        port=int(os.getenv("SCRAPER_PORT", "8000")),
        reload=env_flag("SCRAPER_RELOAD", True),
        reload_dirs=["app"],
        env_file=env_file,
        # Uvicorn's Windows reload setup selects SelectorEventLoop, which
        # cannot create the subprocess used by Playwright. Leaving
        # loop setup untouched preserves Python's ProactorEventLoop.
        loop="none" if sys.platform == "win32" else "auto",
    )
