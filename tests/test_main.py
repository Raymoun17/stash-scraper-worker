import unittest

from app.main import (
    FetchRequest,
    ScraperError,
    fetch_with_browser,
    safe_url_for_log,
)


PRODUCT_URL = "https://www2.hm.com/en_ca/productpage.1234567890.html"


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

    async def content(self) -> str:
        return self.html


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

    async def test_returns_metadata_only_page_for_downstream_extraction(self):
        html = "<html><head><title>Product</title></head></html>"
        browser = FakeBrowser(html)
        payload = FetchRequest(
            url=PRODUCT_URL,
            allowedHosts=["www2.hm.com"],
            waitAfterDomMs=0,
        )

        result = await fetch_with_browser(browser, payload)

        self.assertEqual(result["html"], html)
        self.assertEqual(set(result), {"requestedUrl", "finalUrl", "html"})
        self.assertTrue(browser.context.closed)


if __name__ == "__main__":
    unittest.main()
