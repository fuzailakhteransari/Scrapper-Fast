from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections import OrderedDict
from urllib.parse import quote, urlsplit
from urllib.robotparser import RobotFileParser

import aiohttp
from bs4 import BeautifulSoup

from .config import Settings
from .models import FetchResult
from .utils import origin, url_variants, validate_public_host

LOGGER = logging.getLogger(__name__)

TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
BLOCK_STATUSES = {401, 403, 407, 418, 429, 451, 503}
BLOCK_MARKERS = (
    "access denied",
    "attention required",
    "bot detection",
    "captcha",
    "cf-chl-",
    "checking your browser",
    "cloudflare ray id",
    "enable javascript and cookies",
    "incapsula",
    "perimeterx",
    "press and hold",
    "request blocked",
    "security check",
    "unusual traffic",
    "verify you are human",
)


def _looks_blocked(status: int | None, html: str) -> bool:
    if status in BLOCK_STATUSES:
        return True
    sample = html[:200_000].lower()
    return any(marker in sample for marker in BLOCK_MARKERS)


def _looks_like_js_shell(html: str) -> bool:
    if not html:
        return False
    soup = BeautifulSoup(html[:1_000_000], "lxml")
    for element in soup(["script", "style", "noscript", "template", "svg"]):
        element.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    script_count = html.lower().count("<script")
    root_markers = any(
        marker in html.lower()
        for marker in ('id="root"', "id='root'", 'id="app"', "id='app'", "__next_data__")
    )
    return len(text) < 100 and script_count >= 3 and root_markers


class DirectFetcher:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        timeout = aiohttp.ClientTimeout(
            total=self.settings.timeout_seconds,
            connect=self.settings.connect_timeout_seconds,
            sock_read=self.settings.timeout_seconds,
        )
        connector = aiohttp.TCPConnector(
            limit=self.settings.concurrency * 2,
            limit_per_host=max(2, self.settings.per_site_concurrency),
            ttl_dns_cache=600,
            enable_cleanup_closed=True,
        )
        self.session = aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            auto_decompress=True,
            headers={
                "User-Agent": self.settings.user_agent,
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "text/plain;q=0.8,*/*;q=0.5"
                ),
                "Accept-Language": "en-US,en;q=0.8",
                "Cache-Control": "no-cache",
            },
        )

    async def close(self) -> None:
        if self.session:
            await self.session.close()

    async def fetch(self, url: str) -> FetchResult:
        if self.session is None:
            raise RuntimeError("DirectFetcher.start() was not called")
        last = FetchResult(requested_url=url, error="not attempted")
        for attempt in range(self.settings.retries + 1):
            started = time.perf_counter()
            try:
                async with self.session.get(
                    url,
                    allow_redirects=True,
                    max_redirects=10,
                    ssl=True,
                ) as response:
                    content_type = response.headers.get("Content-Type", "")
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        body.extend(chunk)
                        if len(body) > self.settings.max_response_bytes:
                            raise ValueError("response exceeds configured size limit")
                    encoding = response.charset or "utf-8"
                    html = body.decode(encoding, errors="replace")
                    supported_content = (
                        not content_type
                        or content_type.lower().startswith("text/")
                        or any(
                            marker in content_type.lower()
                            for marker in ("html", "xhtml", "xml")
                        )
                    )
                    elapsed = int((time.perf_counter() - started) * 1000)
                    final_url = str(response.url)
                    last = FetchResult(
                        requested_url=url,
                        final_url=final_url,
                        status=response.status,
                        html=html
                        if response.status < 500 and supported_content
                        else "",
                        tier="direct",
                        error=""
                        if supported_content
                        else f"unsupported content type: {content_type}",
                        elapsed_ms=elapsed,
                        blocked=_looks_blocked(response.status, html),
                        content_type=content_type,
                    )
                    if response.status not in TRANSIENT_STATUSES:
                        return last
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                UnicodeError,
                ValueError,
            ) as exc:
                elapsed = int((time.perf_counter() - started) * 1000)
                last = FetchResult(
                    requested_url=url,
                    tier="direct",
                    error=f"{type(exc).__name__}: {exc}",
                    elapsed_ms=elapsed,
                )
            if attempt < self.settings.retries:
                await asyncio.sleep((0.35 * (2**attempt)) + random.random() * 0.25)
        return last


class WebUnlockerFetcher:
    ENDPOINT = "https://api.brightdata.com/request"

    def __init__(self, settings: Settings, session_getter):
        self.settings = settings
        self._session_getter = session_getter

    async def fetch(self, url: str) -> FetchResult:
        session = self._session_getter()
        if session is None:
            return FetchResult(
                requested_url=url, tier="web_unlocker", error="HTTP session unavailable"
            )
        started = time.perf_counter()
        payload = {
            "zone": self.settings.brightdata_unlocker_zone,
            "url": url,
            "format": "raw",
        }
        headers = {
            "Authorization": f"Bearer {self.settings.brightdata_api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with session.post(
                self.ENDPOINT,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(
                    total=max(60, self.settings.timeout_seconds * 4)
                ),
            ) as response:
                body = await response.read()
                if len(body) > self.settings.max_response_bytes:
                    raise ValueError("response exceeds configured size limit")
                html = body.decode(response.charset or "utf-8", errors="replace")
                elapsed = int((time.perf_counter() - started) * 1000)
                return FetchResult(
                    requested_url=url,
                    final_url=url,
                    status=response.status,
                    html=html if response.status < 400 else "",
                    tier="web_unlocker",
                    error="" if response.status < 400 else html[:500],
                    elapsed_ms=elapsed,
                    blocked=_looks_blocked(response.status, html),
                    rendered=True,
                    content_type=response.headers.get("Content-Type", ""),
                )
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            return FetchResult(
                requested_url=url,
                tier="web_unlocker",
                error=f"{type(exc).__name__}: {exc}",
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )


class BrowserFetcher:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._playwright = None
        self._browser = None
        self._start_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(settings.browser_concurrency)

    async def _ensure_started(self) -> None:
        if self._browser is not None:
            return
        async with self._start_lock:
            if self._browser is not None:
                return
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise RuntimeError(
                    "Playwright is not installed; install requirements.txt"
                ) from exc
            self._playwright = await async_playwright().start()
            username = quote(self.settings.brightdata_browser_username, safe="")
            password = quote(self.settings.brightdata_browser_password, safe="")
            endpoint = (
                f"wss://{username}:{password}@"
                f"{self.settings.brightdata_browser_host}:"
                f"{self.settings.brightdata_browser_port}"
            )
            self._browser = await self._playwright.chromium.connect_over_cdp(
                endpoint, timeout=90_000
            )

    async def fetch(self, url: str) -> FetchResult:
        started = time.perf_counter()
        async with self._semaphore:
            context = None
            page = None
            try:
                await self._ensure_started()
                context = await self._browser.new_context(
                    user_agent=self.settings.user_agent,
                    ignore_https_errors=False,
                )
                page = await context.new_page()
                response = await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=max(60_000, self.settings.timeout_seconds * 4_000),
                )
                try:
                    await page.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:
                    pass
                html = await page.content()
                if len(html.encode("utf-8")) > self.settings.max_response_bytes:
                    raise ValueError("rendered response exceeds configured size limit")
                final_url = page.url
                status = response.status if response else 200
                return FetchResult(
                    requested_url=url,
                    final_url=final_url,
                    status=status,
                    html=html,
                    tier="browser",
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                    blocked=_looks_blocked(status, html),
                    rendered=True,
                    content_type="text/html",
                )
            except Exception as exc:
                return FetchResult(
                    requested_url=url,
                    tier="browser",
                    error=f"{type(exc).__name__}: {exc}",
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                )
            finally:
                if page:
                    await page.close()
                if context:
                    await context.close()

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None


class RobotsCache:
    def __init__(self, direct: DirectFetcher, max_entries: int = 5000):
        self.direct = direct
        self.max_entries = max_entries
        self._cache: OrderedDict[str, RobotFileParser | None] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}

    async def allowed(self, url: str, user_agent: str) -> bool:
        site_origin = origin(url)
        parser = self._cache.get(site_origin)
        if site_origin in self._cache:
            self._cache.move_to_end(site_origin)
            return parser is None or parser.can_fetch(user_agent, url)
        lock = self._locks.setdefault(site_origin, asyncio.Lock())
        async with lock:
            if site_origin in self._cache:
                parser = self._cache[site_origin]
                return parser is None or parser.can_fetch(user_agent, url)
            result = await self.direct.fetch(site_origin + "/robots.txt")
            parser = None
            if result.ok and result.html:
                parser = RobotFileParser()
                parser.set_url(site_origin + "/robots.txt")
                parser.parse(result.html.splitlines())
            self._cache[site_origin] = parser
            if len(self._cache) > self.max_entries:
                self._cache.popitem(last=False)
            self._locks.pop(site_origin, None)
            return parser is None or parser.can_fetch(user_agent, url)


class FetchManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.direct = DirectFetcher(settings)
        self.unlocker = WebUnlockerFetcher(settings, lambda: self.direct.session)
        self.browser = BrowserFetcher(settings)
        self.robots = RobotsCache(self.direct)

    async def start(self) -> None:
        await self.direct.start()

    async def close(self) -> None:
        await self.browser.close()
        await self.direct.close()

    async def fetch(
        self,
        url: str,
        *,
        homepage: bool = False,
        allow_browser: bool = True,
        check_robots: bool = True,
    ) -> FetchResult:
        await validate_public_host(url)
        if (
            check_robots
            and self.settings.respect_robots_txt
            and not await self.robots.allowed(url, "*")
        ):
            return FetchResult(
                requested_url=url,
                tier="robots",
                error="disallowed by robots.txt",
            )

        variants = url_variants(url) if homepage else [url]
        direct_results: list[FetchResult] = []
        for candidate in variants:
            result = await self.direct.fetch(candidate)
            direct_results.append(result)
            if result.ok and not result.blocked and not _looks_like_js_shell(result.html):
                return result
            if result.status == 404 and not homepage:
                return result

        best_direct = max(
            direct_results,
            key=lambda item: (
                item.ok,
                bool(item.html),
                item.status or 0,
                -item.elapsed_ms,
            ),
        )
        target = best_direct.final_url or variants[0]

        unlocker_result: FetchResult | None = None
        if self.settings.use_web_unlocker and self.settings.web_unlocker_ready:
            unlocker_result = await self.unlocker.fetch(target)
            if (
                unlocker_result.ok
                and not unlocker_result.blocked
                and not _looks_like_js_shell(unlocker_result.html)
            ):
                return unlocker_result

        if (
            allow_browser
            and self.settings.use_browser
            and self.settings.browser_ready
        ):
            browser_result = await self.browser.fetch(target)
            if browser_result.ok and not browser_result.blocked:
                return browser_result
            if browser_result.html:
                return browser_result

        if unlocker_result and (unlocker_result.ok or unlocker_result.html):
            return unlocker_result
        return best_direct
