"""Public page discovery and rendered-text extraction. No content API decoding."""
import asyncio
import ipaddress
import re
import socket
from contextlib import asynccontextmanager
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from playwright.async_api import TimeoutError as BrowserTimeout


DEFAULTS = {
    "title": ".page-title h2",
    "row": ".serial-list .list-item, #serial-move .list-item",
    "link": "a.item-subject, .wr-subject a",
    "number": ".wr-num",
    "pager": ".theme-episode-pager a[href], .pg_wrap a[href]",
    "content": "[data-theme-novel-content], #novel_content, .novel-content, #bo_v_con, article",
}
HEADERS = {"User-Agent": "NewtLocalTest/0.1 (single-request text collector)"}


def normalize_url(value: str) -> str:
    p = urlsplit(value.strip())
    if p.scheme not in ("https", "http") or not p.hostname or p.username or p.password:
        raise ValueError("http 또는 https 형식의 공개 페이지 URL을 입력하세요.")
    if p.port not in (None, 80, 443):
        raise ValueError("기본 웹 포트(80, 443)만 지원합니다.")
    return urlunsplit((p.scheme, p.netloc.lower(), p.path or "/", p.query, ""))


async def validate_public(url: str, cache=None):
    url = normalize_url(url)
    host = urlsplit(url).hostname
    if cache is not None and host in cache:
        return url
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("사이트 주소를 찾지 못했습니다. URL과 인터넷 연결을 확인하세요.") from exc
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("로컬·사설망 주소는 수집할 수 없습니다.")
    if cache is not None:
        cache.add(host)
    return url


def clean_text(value):
    value = value.replace("\r\n", "\n").replace("\xa0", " ")
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def parse_list(html: str, url: str, selectors: dict):
    soup = BeautifulSoup(html, "html.parser")
    title_el = soup.select_one(selectors["title"])
    title = title_el.get_text(" ", strip=True) if title_el else ""
    chapters = []
    for row in soup.select(selectors["row"]):
        link = row.select_one(selectors["link"])
        number = row.select_one(selectors["number"])
        if not link or not link.get("href") or not number:
            continue
        # Use the list's episode number, never a number inside a subtitle.
        match = re.search(r"\d+", number.get_text(" ", strip=True).replace(",", ""))
        if not match:
            continue
        href = normalize_url(urljoin(url, link["href"]))
        if urlsplit(href).netloc != urlsplit(url).netloc:
            continue
        chapters.append({"number": int(match[0]), "title": link.get_text(" ", strip=True), "url": href})
    pages = []
    for link in soup.select(selectors["pager"]):
        try:
            href = normalize_url(urljoin(url, link["href"]))
        except ValueError:
            continue
        p, base = urlsplit(href), urlsplit(url)
        if p.netloc == base.netloc and p.path.rstrip("/") == base.path.rstrip("/"):
            pages.append(href)
    return title, chapters, list(dict.fromkeys(pages))


async def fetch_html(client, url, cache):
    for _ in range(6):
        await validate_public(url, cache)
        response = await client.get(url)
        if response.is_redirect:
            url = normalize_url(urljoin(url, response.headers["location"]))
            continue
        response.raise_for_status()
        if len(response.content) > 8_000_000:
            raise ValueError("목록 페이지가 너무 큽니다.")
        return response.text, url
    raise ValueError("페이지 이동이 너무 많습니다.")


async def discover(url, custom):
    selectors = {**DEFAULTS, **{k: v.strip() for k, v in custom.items() if v.strip()}}
    url = normalize_url(url)
    pending, visited, cache, chapters = [url], set(), set(), {}
    title = ""
    async with httpx.AsyncClient(headers=HEADERS, timeout=30, trust_env=False) as client:
        while pending:
            current = pending.pop(0)
            if current in visited:
                continue
            if len(visited) >= 100:
                raise ValueError("목록 100페이지를 초과했습니다. 범위를 나누어 주세요.")
            html, final_url = await fetch_html(client, current, cache)
            visited.update((current, final_url))
            found_title, found, pages = parse_list(html, final_url, selectors)
            title = title or found_title
            for chapter in found:
                chapters[chapter["url"]] = chapter
            pending.extend(p for p in pages if p not in visited and p not in pending)
            if pending:
                await asyncio.sleep(0.6)
    if not chapters:
        raise ValueError("회차 목록을 찾지 못했습니다. 목록 URL인지 확인하거나 고급 설정의 추출 규칙을 조정하세요.")
    result = sorted(chapters.values(), key=lambda c: (c["number"], c["url"]))
    numbers = [c["number"] for c in result]
    if len(numbers) != len(set(numbers)):
        raise ValueError("서로 다른 링크에 같은 회차 번호가 있습니다. 목록 추출 규칙을 확인하세요.")
    return {"title": title or "제목 없는 작품", "url": url, "chapters": result,
            "pages": len(visited), "selectors": selectors}


# Read the page's normally rendered open shadow DOM; the target site renders
# paragraphs in a shadow root after its own loading sequence completes.
READ_TEXT = """el => {
  function read(node) {
    if (node.nodeType === 3) return node.textContent;
    if (['STYLE','SCRIPT','NOSCRIPT'].includes(node.tagName)) return '';
    if (node.nodeType === 1 && (node.hidden || getComputedStyle(node).display === 'none')) return '';
    if (node.tagName === 'BR') return '\\n';
    const root = node.shadowRoot || node;
    const text = Array.from(root.childNodes).map(read).join('');
    return ['P','DIV','SECTION','H1','H2','H3','LI'].includes(node.tagName) ? text + '\\n\\n' : text;
  }
  return read(el).trim();
}"""
PLACEHOLDERS = ("본문 불러오는 중", "본문을 불러오는 중", "잠시만 기다", "본문이 아직 준비", "본문을 불러오지 못", "본문을 불러올 수", "로그인 후", "접근 권한", "인증이 필요")


class SiteAccessError(ValueError):
    """Keep HTTP refusals distinct from explicit login/verification prompts."""

    def __init__(self, message, *, status_code=None, requires_verification=False):
        super().__init__(message)
        self.status_code = status_code
        self.requires_verification = requires_verification


ACCESS_MESSAGES = ("일일 조회 인증", "보안 검증에 실패", "광고 검증 후", "로그인이 필요", "로그인 후",
                   "접근 권한", "인증이 필요", "captcha", "too many requests", "access denied")


def access_message(text):
    # Check short status banners, not matching words inside a novel paragraph.
    return len(text) < 500 and any(term in text.lower() for term in ACCESS_MESSAGES)


@asynccontextmanager
async def reader():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(locale="ko-KR", viewport={"width": 1280, "height": 900})
        cache = set()

        async def guard(route):
            try:
                await validate_public(route.request.url, cache)
            except Exception:
                await route.abort()
                return
            await route.continue_()

        await context.route("**/*", guard)
        page = await context.new_page()
        page.set_default_timeout(15_000)
        try:
            yield page
        finally:
            await context.close()
            await browser.close()


async def extract(page, chapter, selector):
    await validate_public(chapter["url"])
    responses, script_errors = [], []

    def on_response(response):
        path = urlsplit(response.url).path
        if path.startswith("/api/") and response.status >= 400:
            responses.append(response)
            del responses[:-10]

    def on_error(error):
        script_errors.append(str(error)[:200])
        del script_errors[:-3]

    page.on("response", on_response)
    page.on("pageerror", on_error)
    try:
        for attempt in range(2):
            responses.clear()
            script_errors.clear()
            try:
                response = await page.goto(chapter["url"], wait_until="domcontentloaded", timeout=45_000)
                status = response.status if response else None
                if status in (401, 402, 403, 429):
                    requires_verification = getattr(response, "headers", {}).get("cf-mitigated") == "challenge"
                    if status == 403:
                        try:
                            error_page = BeautifulSoup((await response.text())[:100_000], "html.parser")
                            for tag in error_page.select("script, style, nav, header, footer, form, template"):
                                tag.decompose()
                            text = error_page.get_text(" ", strip=True).lower()
                            requires_verification = requires_verification or any(marker in text for marker in (
                                "로그인이 필요", "로그인 후 이용", "로그인 후 다시", "일일 조회 인증", "인증을 완료",
                                "보안 인증이 필요", "verify you are human", "complete the captcha", "captcha_required",
                                "authentication required", "please log in", "please sign in"))
                        except Exception:
                            pass
                    detail = " 로그인 또는 보안 인증 안내가 표시되었습니다." if requires_verification else " 원본 페이지의 로그인·인증 또는 조회 제한을 확인하세요."
                    raise SiteAccessError(f"사이트가 접근을 제한했습니다(HTTP {status})." + detail,
                                          status_code=status, requires_verification=requires_verification)
                if status and status >= 400:
                    raise ValueError(f"회차 페이지 응답 오류(HTTP {status}).")
                return await read_rendered(page, selector, status, responses, script_errors)
            except SiteAccessError:
                raise
            except (ValueError, BrowserTimeout):
                if attempt:
                    raise
                # One ordinary reload in the SAME page/context. Keep cookies,
                # session storage and site limits; no fresh-identity retry.
                await asyncio.sleep(3)
    finally:
        page.remove_listener("response", on_response)
        page.remove_listener("pageerror", on_error)


async def read_rendered(page, selector, status, responses, script_errors):
    candidates = page.locator(selector)
    # Wait for a stable body so a partial asynchronous render is not saved.
    last, stable, last_seen = "", 0, ""
    for _ in range(60):
        for item in await candidates.all():
            if not await item.is_visible():
                continue
            text = clean_text(await item.evaluate(READ_TEXT))
            last_seen = text
            if access_message(text):
                raise SiteAccessError("사이트 안내: " + text, requires_verification=True)
            if not text or any(term in text[:240] for term in PLACEHOLDERS):
                continue
            if text == last:
                stable += 1
            else:
                last, stable = text, 0
            if stable >= 2:
                return text
            break
        await asyncio.sleep(0.5)
    # An HTTP 200 shell can hide a failed content request. Report the real
    # response instead of describing every failure as an empty chapter.
    for response in reversed(responses):
        code = ""
        try:
            payload = await response.json()
            if isinstance(payload, dict):
                code = str(payload.get("error", ""))[:120]
        except Exception:
            pass
        path = urlsplit(response.url).path
        if response.status in (401, 402, 403, 429):
            raise SiteAccessError(f"사이트 응답 HTTP {response.status} ({path}{': ' + code if code else ''}). 원본 페이지의 인증·접근 제한을 확인하세요.",
                                  status_code=response.status, requires_verification=True)
    elements = await candidates.all()
    if not elements:
        visible_page = clean_text(await page.locator("body").inner_text(timeout=3000))[:500]
        if access_message(visible_page):
            raise SiteAccessError("사이트 안내: " + visible_page, requires_verification=True)
    reason = f"본문 로딩 시간 초과(페이지 HTTP {status}, 본문 요소 {len(elements)}개)."
    reason += " 페이지: " + (await page.title() or "제목 없음")[:100]
    if last_seen and len(last_seen) < 500:
        reason += " / 표시 메시지: " + last_seen
    if script_errors:
        reason += " / 스크립트 오류: " + script_errors[-1]
    raise ValueError(reason)
