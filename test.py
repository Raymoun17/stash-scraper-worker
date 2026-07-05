"""Render a URL with the scraper and print its HTML.

Usage: python test.py <url>
"""

import argparse
import asyncio
import os
import sys
from urllib.parse import urlsplit

from app.main import BrowserRuntime, FetchRequest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Public HTTP(S) page to render")
    parser.add_argument(
        "--wait-ms",
        type=int,
        default=3_000,
        help="Milliseconds to wait after DOMContentLoaded (default: 3000)",
    )
    parser.add_argument(
        "--backend",
        choices=("playwright-stealth",),
        default=os.getenv("SCRAPER_BACKEND", "playwright-stealth"),
        help="Browser backend (default: SCRAPER_BACKEND or playwright-stealth)",
    )
    return parser.parse_args()


async def scrape(url: str, wait_ms: int) -> None:
    hostname = urlsplit(url).hostname
    if not hostname:
        raise ValueError("URL must include a hostname")

    runtime = BrowserRuntime()
    await runtime.start()
    try:
        result = await runtime.fetch(
            FetchRequest(
                url=url,
                allowedHosts=[hostname],
                timeoutMs=30_000,
                waitAfterDomMs=wait_ms,
            )
        )
        print(result["html"])
    finally:
        await runtime.close()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args()
    os.environ["SCRAPER_BACKEND"] = args.backend
    try:
        asyncio.run(scrape(args.url, args.wait_ms))
    except Exception as error:
        print(f"Scrape failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
