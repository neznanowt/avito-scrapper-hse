"""Браузер на Playwright: один поток, паузы между запросами, сохранённый профиль (куки)."""

from __future__ import annotations

import logging
import random
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from .config import BrowserConfig, PacingConfig
from .models import DeadlineReached, FetchResult
from .utils import now_msk

log = logging.getLogger("realty")


class FetchError(Exception):
    pass


def _proxy(proxy: str | None) -> dict | None:
    if not proxy:
        return None
    p = urlsplit(proxy)
    out = {"server": f"{p.scheme}://{p.hostname}:{p.port}" if p.port else f"{p.scheme}://{p.hostname}"}
    if p.username:
        out["username"] = p.username
        out["password"] = p.password or ""
    return out


class BrowserSession:
    def __init__(self, bcfg: BrowserConfig, pacing: PacingConfig, profile_dir: Path, deadline: float | None = None):
        self.bcfg = bcfg
        self.pacing = pacing
        self.profile_dir = Path(profile_dir)
        self.deadline = deadline
        self._pw = None
        self.ctx = None
        self.page = None
        self._last_end: float | None = None
        self.pages_fetched = 0
        self._blocked_types = set(bcfg.block_resources or [])

    # ---- жизненный цикл ----
    def __enter__(self):
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        ua = self.bcfg.user_agent or (self._headless_ua() if self.bcfg.headless else None)
        kw = dict(
            user_data_dir=str(self.profile_dir),
            headless=self.bcfg.headless,
            locale=self.bcfg.locale,
            timezone_id=self.bcfg.timezone,
            viewport={"width": self.bcfg.viewport_width, "height": self.bcfg.viewport_height},
            args=["--disable-blink-features=AutomationControlled"],
        )
        if self.bcfg.channel:
            kw["channel"] = self.bcfg.channel
        if ua:
            kw["user_agent"] = ua
        if self.bcfg.proxy:
            kw["proxy"] = _proxy(self.bcfg.proxy)
        self.ctx = self._pw.chromium.launch_persistent_context(**kw)
        self.ctx.set_default_navigation_timeout(self.bcfg.page_timeout_s * 1000)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        if self._blocked_types:
            self.page.route("**/*", self._route)
        log.info("Браузер запущен (channel=%s, headless=%s)", self.bcfg.channel, self.bcfg.headless)
        return self

    def __exit__(self, *exc):
        try:
            if self.ctx:
                self.ctx.close()
        finally:
            if self._pw:
                self._pw.stop()

    def _headless_ua(self) -> str | None:
        try:
            b = self._pw.chromium.launch(headless=True, channel=self.bcfg.channel or None)
            ua = b.new_page().evaluate("navigator.userAgent")
            b.close()
            return ua.replace("HeadlessChrome", "Chrome")
        except PlaywrightError:
            return None

    def _route(self, route):
        if route.request.resource_type in self._blocked_types:
            route.abort()
        else:
            route.continue_()

    # ---- паузы ----
    def sleep(self, seconds: float):
        if self.deadline is not None and time.monotonic() + seconds > self.deadline:
            raise DeadlineReached()
        time.sleep(max(0.0, seconds))

    def _pace(self):
        p = self.pacing
        if self._last_end is not None:
            wait = random.uniform(p.delay_min_s, p.delay_max_s) - (time.monotonic() - self._last_end)
            if wait > 0:
                self.sleep(wait)
        if p.long_pause_every and self.pages_fetched and self.pages_fetched % p.long_pause_every == 0:
            pause = random.uniform(p.long_pause_min_s, p.long_pause_max_s)
            log.info("Длинная пауза %.0f с после %d страниц", pause, self.pages_fetched)
            self.sleep(pause)

    # ---- загрузка ----
    def fetch(self, url: str, wait_selector: str | None = None) -> FetchResult:
        if self.deadline is not None and time.monotonic() > self.deadline:
            raise DeadlineReached()
        self._pace()
        t0 = time.monotonic()
        fetched_at = now_msk()
        try:
            resp = self.page.goto(url, wait_until="domcontentloaded")
            if wait_selector:
                try:
                    self.page.wait_for_selector(wait_selector, timeout=8000, state="attached")
                except PlaywrightTimeout:
                    pass
            html = self.page.content()
            title = self.page.title()
        except (PlaywrightTimeout, PlaywrightError) as e:
            raise FetchError(f"{type(e).__name__}: {str(e).splitlines()[0][:300]}") from e
        finally:
            self._last_end = time.monotonic()
            self.pages_fetched += 1
        return FetchResult(url=url, final_url=self.page.url, status=resp.status if resp else None,
                           html=html, title=title, fetched_at=fetched_at, elapsed_s=time.monotonic() - t0)

    def current_html(self) -> tuple[str, str]:
        return self.page.content(), self.page.title()

    def screenshot(self, path: Path):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(path), full_page=False)
        except PlaywrightError:
            pass
