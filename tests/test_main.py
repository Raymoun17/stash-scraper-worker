import unittest

from app.main import (
    FetchRequest,
    ScraperError,
    fetch_with_browser,
    is_security_challenge_html,
    log_preview,
    safe_url_for_log,
)


PRODUCT_URL = "https://www2.hm.com/en_ca/productpage.1234567890.html"


class FakeLocator:
    def __init__(self, body_text: str = "Product details") -> None:
        self.body_text = body_text

    @property
    def first(self):
        return self

    async def is_visible(self) -> bool:
        return False

    async def click(self, timeout: int) -> None:
        del timeout

    async def inner_text(self) -> str:
        return self.body_text


class FakePage:
    def __init__(self, html: str) -> None:
        self.html = html
        self.url = PRODUCT_URL

    async def route(self, pattern: str, handler) -> None:
        del pattern, handler

    def on(self, event: str, handler) -> None:
        del event, handler

    async def goto(self, url: str, **_options):
        self.url = url
        return type("Response", (), {"status": 200})()

    async def wait_for_timeout(self, milliseconds: int) -> None:
        del milliseconds

    async def title(self) -> str:
        return "Product"

    async def content(self) -> str:
        return self.html

    def locator(self, selector: str) -> FakeLocator:
        del selector
        return FakeLocator()


class FakeContext:
    def __init__(self, html: str) -> None:
        self.page = FakePage(html)
        self.closed = False

    def set_default_timeout(self, timeout: int) -> None:
        del timeout

    def set_default_navigation_timeout(self, timeout: int) -> None:
        del timeout

    async def new_page(self) -> FakePage:
        return self.page

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self, html: str) -> None:
        self.context = FakeContext(html)
        self.context_options = None

    async def new_context(self, **options) -> FakeContext:
        self.context_options = options
        return self.context


class FetchWithBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_rendered_content_and_closes_context(self):
        browser = FakeBrowser("<html><body>Product</body></html>")
        payload = FetchRequest(
            url=PRODUCT_URL,
            allowedHosts=["www2.hm.com"],
            waitAfterDomMs=0,
        )

        result = await fetch_with_browser(browser, payload)

        self.assertEqual(result["requestedUrl"], PRODUCT_URL)
        self.assertEqual(result["finalUrl"], PRODUCT_URL)
        self.assertIn("Product", result["html"])
        self.assertTrue(browser.context_options["no_viewport"])
        self.assertTrue(browser.context.closed)

    def test_safe_url_for_log_removes_query_and_fragment(self):
        self.assertEqual(
            safe_url_for_log(f"{PRODUCT_URL}?token=secret#details"),
            PRODUCT_URL,
        )

    def test_detects_zara_akamai_interstitial(self):
        html = '<meta http-equiv="refresh" content="5; URL=\'?bm-verify=abc\'">' \
            '<iframe src="/interstitial/ic.html"></iframe>' \
            '<script>xhr.open("POST", "/_sec/verify")</script>'

        self.assertTrue(is_security_challenge_html(html))

    def test_log_preview_normalizes_whitespace(self):
        self.assertEqual(log_preview("  Product\n  price: $59.90  "), "Product price: $59.90")

    async def test_rejects_oversized_html_and_closes_context(self):
        browser = FakeBrowser("<html></html>")
        payload = FetchRequest(
            url=PRODUCT_URL,
            allowedHosts=["www2.hm.com"],
            waitAfterDomMs=0,
            maxHtmlBytes=1,
        )

        with self.assertRaises(ScraperError) as raised:
            await fetch_with_browser(browser, payload)

        self.assertEqual(raised.exception.code, "HTML_TOO_LARGE")
        self.assertTrue(browser.context.closed)

    async def test_rejects_incomplete_metadata_only_page(self):
        browser = FakeBrowser("<html><head><title>Product</title></head></html>")
        payload = FetchRequest(
            url=PRODUCT_URL,
            allowedHosts=["www2.hm.com"],
            waitAfterDomMs=0,
        )

        with self.assertRaises(ScraperError) as raised:
            await fetch_with_browser(browser, payload)

        self.assertEqual(raised.exception.code, "SOURCE_BLOCKED")
        self.assertIn("incomplete page", raised.exception.message)


if __name__ == "__main__":
    unittest.main()
