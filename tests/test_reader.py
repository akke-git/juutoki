"""Browser boundary fixtures: simulate responses without contacting a real site."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import scraper


class Element:
    def __init__(self, text):
        self.text = text

    async def is_visible(self):
        return True

    async def evaluate(self, script):
        return self.text


class Locator:
    def __init__(self, page):
        self.page = page

    async def all(self):
        text = self.page.renders[min(self.page.navigations - 1, len(self.page.renders) - 1)]
        return [Element(text)] if text is not None else []

    async def inner_text(self, **kwargs):
        return "페이지를 불러올 수 없습니다."


class DocumentResponse:
    def __init__(self, status, html, headers):
        self.status = status
        self.html = html
        self.headers = headers

    async def text(self):
        return self.html


class BrowserPage:
    def __init__(self, renders, status=200, html="", headers=None):
        self.renders = renders
        self.status = status
        self.navigations = 0
        self.url = "https://example.com/chapter/82"
        self.html = html
        self.headers = headers or {}

    async def goto(self, url, **kwargs):
        self.navigations += 1
        return DocumentResponse(self.status, self.html, self.headers)

    def locator(self, selector):
        return Locator(self)

    def on(self, *args):
        pass

    def remove_listener(self, *args):
        pass

    async def title(self):
        return "테스트 회차"


class ReaderTests(unittest.IsolatedAsyncioTestCase):
    async def read(self, page):
        # DNS and polling delays are external boundaries; keep extraction real.
        with patch.object(scraper, "validate_public", AsyncMock()), patch.object(scraper.asyncio, "sleep", AsyncMock()):
            return await scraper.extract(page, {"url": page.url}, "article")

    async def test_security_failure_message_is_never_returned_as_a_chapter(self):
        with self.assertRaises(ValueError):
            await self.read(BrowserPage(["본문 보안 검증에 실패했습니다."]))

    async def test_daily_verification_message_is_never_returned_as_a_chapter(self):
        with self.assertRaises(ValueError):
            await self.read(BrowserPage(["일일 조회 인증이 필요합니다. 일반 소설 뷰어에서 인증 후 다시 열어주세요."]))

    async def test_http_rate_limit_is_reported_and_does_not_restart_session(self):
        page = BrowserPage(["본문처럼 보이는 오류 페이지"], status=429)
        with self.assertRaisesRegex(ValueError, "429"):
            await self.read(page)
        self.assertEqual(page.navigations, 1)

    async def test_explicit_login_403_is_identified_in_error_metadata(self):
        page = BrowserPage([None], status=403, html="<h1>로그인이 필요합니다.</h1>")
        with self.assertRaises(scraper.SiteAccessError) as error:
            await self.read(page)
        self.assertTrue(error.exception.requires_verification)

    async def test_explicit_verification_403_is_identified_in_error_metadata(self):
        page = BrowserPage([None], status=403, html="<h1>Verify you are human</h1>")
        with self.assertRaises(scraper.SiteAccessError) as error:
            await self.read(page)
        self.assertTrue(error.exception.requires_verification)

    async def test_generic_document_403_is_distinct_from_verification_prompt(self):
        page = BrowserPage([None], status=403, html="<h1>Forbidden</h1>")
        with self.assertRaises(scraper.SiteAccessError) as error:
            await self.read(page)
        self.assertFalse(error.exception.requires_verification)

    async def test_api_403_refusal_is_identified_in_error_metadata(self):
        page = BrowserPage([None])
        response = SimpleNamespace(status=403, url="https://example.com/api/novel-content",
                                   json=AsyncMock(return_value={"error": "ad_ack_required"}))
        with patch.object(scraper.asyncio, "sleep", AsyncMock()):
            with self.assertRaises(scraper.SiteAccessError) as error:
                await scraper.read_rendered(page, "article", 200, [response], [])
        self.assertTrue(error.exception.requires_verification)

    async def test_transient_missing_body_recovers_with_same_page_reload(self):
        text = "정상적인 첫 문단입니다.\n\n정상적인 둘째 문단입니다."
        page = BrowserPage([None, text])
        self.assertEqual(await self.read(page), text)

    async def test_permanent_missing_body_is_not_silently_treated_as_empty_success(self):
        with self.assertRaisesRegex(ValueError, "본문"):
            await self.read(BrowserPage([None]))
