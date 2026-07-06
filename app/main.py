from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import secrets
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal, Protocol
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright
from playwright_stealth import Stealth


DEFAULT_SERVICE_TOKEN = "dev-secret-change-me"
DEFAULT_LOCALE = "en-CA"
DEFAULT_TIMEZONE = "America/Toronto"
logger = logging.getLogger("uvicorn.error")
logger.setLevel(os.getenv("SCRAPER_LOG_LEVEL", "INFO").upper())


def positive_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)

    if raw_value is None:
        return default

    try:
        value = int(raw_value)
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer") from error

    if value < 1:
        raise RuntimeError(f"{name} must be greater than zero")

    return value


class FetchRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    url: str
    allowed_hosts: list[str] = Field(alias="allowedHosts", min_length=1)
    timeout_ms: int = Field(default=20_000, alias="timeoutMs", ge=100, le=120_000)
    wait_after_dom_ms: int = Field(
        default=1_500, alias="waitAfterDomMs", ge=0, le=30_000
    )
    max_html_bytes: int = Field(
        default=10_000_000, alias="maxHtmlBytes", ge=1, le=50_000_000
    )
    locale: str = Field(default=DEFAULT_LOCALE, min_length=2, max_length=35)
    timezone: str = Field(default=DEFAULT_TIMEZONE, min_length=1, max_length=100)
    wait_until: Literal["commit", "domcontentloaded"] = Field(
        default="domcontentloaded", alias="waitUntil"
    )

    @field_validator("allowed_hosts")
    @classmethod
    def normalize_allowed_hosts(cls, hosts: list[str]) -> list[str]:
        normalized: list[str] = []

        for host in hosts:
            candidate = host.strip().lower().rstrip(".")

            if (
                not candidate
                or ":" in candidate
                or "/" in candidate
                or "@" in candidate
            ):
                raise ValueError("allowedHosts must contain hostnames only")

            if candidate not in normalized:
                normalized.append(candidate)

        return normalized


class ScraperError(Exception):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class BrowserNotInstalledError(RuntimeError):
    pass


class BrowserBackend(Protocol):
    name: str

    async def launch(self) -> Any: ...

    async def close(self) -> None: ...


class PlaywrightStealthBackend:
    name = "playwright-stealth"

    def __init__(self) -> None:
        self._launcher: Any | None = None

    async def launch(self) -> Any:
        proxy_url = os.getenv("SCRAPER_PROXY_URL", "").strip()
        logger.info(
            "Launching browser backend=%s proxy_enabled=%s",
            self.name,
            bool(proxy_url),
        )
        self._launcher = Stealth().use_async(async_playwright())
        playwright = await self._launcher.__aenter__()
        launch_options: dict[str, Any] = {"headless": True}
        if proxy_url:
            launch_options["proxy"] = {"server": proxy_url}
        return await playwright.chromium.launch(**launch_options)

    async def close(self) -> None:
        launcher = self._launcher
        self._launcher = None
        if launcher is not None:
            await launcher.__aexit__(None, None, None)


BACKEND_FACTORIES = {
    PlaywrightStealthBackend.name: PlaywrightStealthBackend,
}


def create_browser_backend() -> BrowserBackend:
    name = os.getenv("SCRAPER_BACKEND", PlaywrightStealthBackend.name).strip().lower()
    factory = BACKEND_FACTORIES.get(name)
    if factory is None:
        supported = ", ".join(sorted(BACKEND_FACTORIES))
        raise RuntimeError(f"Unsupported SCRAPER_BACKEND '{name}'; expected: {supported}")
    return factory()


class BrowserManager:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._backend: BrowserBackend | None = None
        self._browser: Any | None = None

    async def get_browser(self) -> Any:
        async with self._lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser

            if self._backend is not None:
                await self._backend.close()

            backend = create_browser_backend()
            self._backend = backend

            try:
                self._browser = await backend.launch()
            except Exception as error:
                self._backend = None
                self._browser = None

                try:
                    await backend.close()
                except Exception:
                    pass

                if "executable doesn't exist" in str(error).lower():
                    raise BrowserNotInstalledError(
                        f"{backend.name} browser is not installed"
                    ) from error

                raise

            return self._browser

    async def close(self) -> None:
        async with self._lock:
            backend = self._backend
            self._backend = None
            self._browser = None

            if backend is not None:
                try:
                    await backend.close()
                except Exception:
                    logger.exception("Failed to close browser backend cleanly")


class BrowserRuntime:
    """Keeps Playwright on a subprocess-capable event loop on Windows."""

    def __init__(self) -> None:
        self._threaded = sys.platform == "win32"
        self._manager: BrowserManager | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None

    async def start(self) -> None:
        if not self._threaded:
            self._manager = BrowserManager()
            await self._manager.get_browser()
            return

        self._thread = threading.Thread(
            target=self._run_windows_loop,
            name="scraper-browser-proactor",
            daemon=True,
        )
        self._thread.start()
        await asyncio.to_thread(self._ready.wait)

        if self._startup_error is not None:
            raise RuntimeError("Failed to start the browser event loop") from self._startup_error

        if self._loop is None or self._manager is None:
            raise RuntimeError("Browser event loop did not initialize")

        future = asyncio.run_coroutine_threadsafe(
            self._manager.get_browser(),
            self._loop,
        )
        await asyncio.wrap_future(future)

    async def fetch(self, payload: FetchRequest) -> dict[str, str]:
        if not self._threaded:
            return await self._fetch_on_browser_loop(payload)

        if self._loop is None:
            raise RuntimeError("Browser event loop is not running")

        future = asyncio.run_coroutine_threadsafe(
            self._fetch_on_browser_loop(payload),
            self._loop,
        )
        return await asyncio.wrap_future(future)

    async def close(self) -> None:
        manager = self._manager

        if not self._threaded:
            if manager is not None:
                await manager.close()
            return

        loop = self._loop
        thread = self._thread

        if loop is None or thread is None:
            return

        try:
            if manager is not None:
                future = asyncio.run_coroutine_threadsafe(manager.close(), loop)
                await asyncio.wrap_future(future)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            await asyncio.to_thread(thread.join, 10)

    async def _fetch_on_browser_loop(
        self, payload: FetchRequest
    ) -> dict[str, str]:
        if self._manager is None:
            raise RuntimeError("Browser manager is not initialized")

        browser = await self._manager.get_browser()
        return await fetch_with_browser(browser, payload)

    def _run_windows_loop(self) -> None:
        try:
            proactor_loop = getattr(asyncio, "ProactorEventLoop")
            loop = proactor_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._manager = BrowserManager()
        except BaseException as error:
            self._startup_error = error
            self._ready.set()
            return

        self._ready.set()

        try:
            loop.run_forever()
        finally:
            loop.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    max_concurrency = positive_int_env("SCRAPER_MAX_CONCURRENCY", 2)
    app.state.semaphore = asyncio.Semaphore(max_concurrency)
    app.state.browser_runtime = BrowserRuntime()
    await app.state.browser_runtime.start()

    try:
        yield
    finally:
        await app.state.browser_runtime.close()


app = FastAPI(title="Stash scraper worker", lifespan=lifespan)


@app.exception_handler(ScraperError)
async def handle_scraper_error(_request: Request, error: ScraperError):
    return JSONResponse(
        status_code=error.status_code,
        content={"error": {"code": error.code, "message": error.message}},
    )


@app.exception_handler(RequestValidationError)
async def handle_validation_error(_request: Request, _error: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "UPSTREAM_FAILURE",
                "message": "Invalid scraper fetch request",
            }
        },
    )


@app.get("/health")
async def health():
    return {"data": {"status": "ok"}}


def require_service_token(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    expected_token = os.getenv("SCRAPER_SERVICE_TOKEN", DEFAULT_SERVICE_TOKEN)
    scheme, separator, supplied_token = (authorization or "").partition(" ")
    authorized = (
        separator == " "
        and scheme.lower() == "bearer"
        and bool(supplied_token)
        and secrets.compare_digest(supplied_token, expected_token)
    )

    if not authorized:
        raise ScraperError("UNAUTHORIZED", "Invalid scraper service token", 401)


@app.post("/fetch", dependencies=[Depends(require_service_token)])
async def fetch_product(payload: FetchRequest, request: Request):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    started_at = time.monotonic()
    target = safe_url_for_log(payload.url)
    logger.info("Scrape started request_id=%s target=%s", request_id, target)
    async with request.app.state.semaphore:
        try:
            result = await request.app.state.browser_runtime.fetch(payload)
            logger.info(
                "Scrape completed request_id=%s target=%s final_url=%s duration_ms=%d html_bytes=%d",
                request_id,
                target,
                safe_url_for_log(result["finalUrl"]),
                round((time.monotonic() - started_at) * 1000),
                len(result["html"].encode("utf-8")),
            )
            return result
        except ScraperError as error:
            logger.warning(
                "Scrape failed request_id=%s target=%s code=%s status=%d duration_ms=%d message=%s cause=%r",
                request_id,
                target,
                error.code,
                error.status_code,
                round((time.monotonic() - started_at) * 1000),
                error.message,
                error.__cause__,
            )
            raise
        except BrowserNotInstalledError as error:
            raise ScraperError(
                "UPSTREAM_FAILURE",
                f"{error}; install the selected browser binary",
                502,
            ) from error
        except Exception as error:
            logger.exception(
                "Unexpected scraper failure request_id=%s target=%s duration_ms=%d",
                request_id,
                target,
                round((time.monotonic() - started_at) * 1000),
            )
            raise ScraperError(
                "UPSTREAM_FAILURE", "Scraper browser operation failed", 502
            ) from error


async def fetch_with_browser(browser: Any, payload: FetchRequest) -> dict[str, str]:
    requested_url = validate_requested_url(payload.url, payload.allowed_hosts)
    context = None

    try:
        context = await browser.new_context(
            locale=payload.locale,
            timezone_id=payload.timezone,
            no_viewport=True,
            extra_http_headers={
                "Accept-Language": f"{payload.locale},en;q=0.9"
            },
        )
        context.set_default_timeout(payload.timeout_ms)
        context.set_default_navigation_timeout(payload.timeout_ms)

        page = await context.new_page()
        blocked_navigation: str | None = None
        document_status: int | None = None

        def record_response(response: Any) -> None:
            nonlocal document_status
            try:
                if response.request.resource_type == "document":
                    document_status = response.status
            except Exception:
                return

        page.on("response", record_response)

        async def enforce_request_policy(route: Any) -> None:
            nonlocal blocked_navigation
            page_request = route.request
            raw_url = page_request.url

            if is_private_network_url(raw_url):
                if page_request.resource_type == "document":
                    blocked_navigation = raw_url
                await route.abort()
                return

            if page_request.resource_type == "document" and not is_allowed_url(
                raw_url, payload.allowed_hosts
            ):
                blocked_navigation = raw_url
                await route.abort()
                return

            await route.continue_()

        await page.route("**/*", enforce_request_policy)

        try:
            response = await page.goto(
                requested_url,
                wait_until=payload.wait_until,
                timeout=payload.timeout_ms,
            )
        except PlaywrightError as error:
            if blocked_navigation:
                raise ScraperError(
                    "INVALID_FINAL_URL",
                    "Retailer redirected to a disallowed URL",
                    422,
                ) from error
            if document_status in (403, 429):
                raise ScraperError(
                    "SOURCE_BLOCKED",
                    f"Retailer returned HTTP {document_status}",
                    502,
                ) from error
            raise

        status = response.status if response else None

        if status in (403, 429):
            raise ScraperError(
                "SOURCE_BLOCKED", f"Retailer returned HTTP {status}", 502
            )

        if status is not None and status >= 400:
            raise ScraperError(
                "UPSTREAM_FAILURE", f"Retailer returned HTTP {status}", 502
            )

        await page.wait_for_timeout(payload.wait_after_dom_ms)
        html = await page.content()

        final_url = page.url
        logger.info(
            "Scraped page target=%s final_url=%s html_bytes=%d",
            safe_url_for_log(requested_url),
            safe_url_for_log(final_url),
            len(html.encode("utf-8")),
        )

        if not is_allowed_url(final_url, payload.allowed_hosts):
            raise ScraperError(
                "INVALID_FINAL_URL",
                "Retailer returned a final URL outside allowedHosts",
                422,
            )

        if len(html.encode("utf-8")) > payload.max_html_bytes:
            raise ScraperError(
                "HTML_TOO_LARGE",
                "Rendered product page exceeded the content size limit",
                422,
            )

        return {
            "requestedUrl": requested_url,
            "finalUrl": final_url,
            "html": html,
        }
    except ScraperError:
        raise
    except PlaywrightTimeoutError as error:
        raise ScraperError(
            "INTEGRATION_TIMEOUT", "Retailer navigation timed out", 504
        ) from error
    except PlaywrightError as error:
        if "timeout" in str(error).lower():
            raise ScraperError(
                "INTEGRATION_TIMEOUT", "Retailer navigation timed out", 504
            ) from error

        raise ScraperError(
            "UPSTREAM_FAILURE", "Failed to load the retailer page", 502
        ) from error
    except Exception as error:
        raise ScraperError(
            "UPSTREAM_FAILURE", "Failed to load the retailer page", 502
        ) from error
    finally:
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass


def validate_requested_url(raw_url: str, allowed_hosts: list[str]) -> str:
    if not is_allowed_url(raw_url, allowed_hosts) or is_private_network_url(raw_url):
        raise ScraperError(
            "UPSTREAM_FAILURE",
            "Requested URL is not an allowed HTTP(S) retailer URL",
            422,
        )

    return raw_url


def safe_url_for_log(raw_url: str) -> str:
    """Retain useful navigation context without logging query strings or fragments."""
    try:
        parsed = urlsplit(raw_url)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    except ValueError:
        return "<invalid-url>"


def is_allowed_url(raw_url: str, allowed_hosts: list[str]) -> bool:
    try:
        parsed = urlsplit(raw_url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return False

    return (
        parsed.scheme in ("http", "https")
        and bool(hostname)
        and hostname in allowed_hosts
        and parsed.username is None
        and parsed.password is None
    )


def is_private_network_url(raw_url: str) -> bool:
    try:
        parsed = urlsplit(raw_url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return True

    if parsed.scheme in ("data", "blob"):
        return False

    if parsed.scheme not in ("http", "https") or not hostname:
        return True

    if (
        hostname == "localhost"
        or hostname.endswith(".localhost")
        or hostname.endswith(".local")
        or hostname.endswith(".internal")
        or "." not in hostname
    ):
        return True

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False

    return not address.is_global
