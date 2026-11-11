"""Браузер на Playwright: один поток, паузы между запросами.

Два режима:
- launch  — сборщик сам запускает браузер со своим профилем (сервер, Colab);
- connect — сборщик подключается к отдельному окну Google Chrome на этом компьютере через порт отладки
  и работает в своей вкладке. Запросы идут с IP компьютера, браузер — настоящий Chrome с накопленными
  куками, а проверку «я не робот», если она появится, человек проходит прямо в окне.
  Профиль Chrome — отдельный (не основной): к основному профилю Chrome с версии 136 подключаться не даёт,
  да и открывать порт отладки на профиле с почтой и банком незачем.
"""

from __future__ import annotations

import json
import logging
import os
import random
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from .config import BrowserConfig, PacingConfig
from .models import BrowserClosed, BrowserUnavailable, DeadlineReached, FetchResult
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


# ---------- Chrome на этом компьютере (режим connect) ----------

def find_chrome() -> str | None:
    cands: list[Path] = []
    if os.name == "nt":
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                cands.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
    elif sys.platform == "darwin":
        cands.append(Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
    for name in ("google-chrome", "google-chrome-stable", "chrome", "chromium", "chromium-browser"):
        w = shutil.which(name)
        if w:
            cands.append(Path(w))
    return next((str(c) for c in cands if c.exists()), None)


def default_profile_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") if os.name == "nt" else None
    return Path(base) / "AvitoScraperChrome" if base else Path.home() / ".avito-scraper-chrome"


def chrome_is_listening(cdp_url: str, timeout: float = 1.5) -> bool:
    """Отвечает ли Chrome на порту отладки (без прокси — это локальный адрес)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(cdp_url.rstrip("/") + "/json/version", timeout=timeout) as r:
            return "Browser" in json.loads(r.read().decode() or "{}")
    except (OSError, ValueError):
        return False


def start_chrome(bcfg: BrowserConfig, url: str = "about:blank") -> None:
    """Запускает отдельное окно Chrome с профилем сборщика и портом отладки. Окно живёт и после сбора."""
    exe = bcfg.chrome_path or find_chrome()
    if not exe:
        raise BrowserUnavailable("Не нашёл Google Chrome. Установите его или укажите путь в config.yaml: "
                                 "browser.chrome_path")
    port = urlsplit(bcfg.cdp_url).port or 9222
    profile = Path(bcfg.connect_profile_dir) if bcfg.connect_profile_dir else default_profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    args = [exe, f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
            "--no-first-run", "--no-default-browser-check", *bcfg.chrome_args, url]
    kw: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    log.info("Запускаю Chrome для сборщика: профиль %s, порт %d", profile, port)
    subprocess.Popen(args, **kw)
    for _ in range(60):
        if chrome_is_listening(bcfg.cdp_url):
            return
        time.sleep(0.5)
    raise BrowserUnavailable(
        f"Chrome запустился, но не отвечает на {bcfg.cdp_url}. Возможно, окно с этим профилем уже открыто "
        "без порта отладки — закройте все окна Chrome сборщика и запустите снова.")


class BrowserSession:
    def __init__(self, bcfg: BrowserConfig, pacing: PacingConfig, profile_dir: Path, deadline: float | None = None):
        self.bcfg = bcfg
        self.pacing = pacing
        self.profile_dir = Path(profile_dir)
        self.deadline = deadline
        self.connect = bcfg.mode == "connect"
        self._pw = None
        self._browser = None
        self.ctx = None
        self.page = None
        self._last_end: float | None = None
        self.pages_fetched = 0
        # в чужом (пользовательском) окне ничего не режем — пусть страница грузится как у человека
        self._blocked_types = set() if self.connect else set(bcfg.block_resources or [])

    # ---- жизненный цикл ----
    def __enter__(self):
        if self.bcfg.mode not in ("launch", "connect"):
            raise ValueError(f"browser.mode должен быть launch или connect, а не {self.bcfg.mode!r}")
        self._pw = sync_playwright().start()
        try:
            self._connect() if self.connect else self._launch()
        except Exception:
            self._pw.stop()
            raise
        return self

    def _launch(self):
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        ua = self.bcfg.user_agent or (self._headless_ua() if self.bcfg.headless else None)
        kw = {
            "user_data_dir": str(self.profile_dir),
            "headless": self.bcfg.headless,
            "locale": self.bcfg.locale,
            "timezone_id": self.bcfg.timezone,
            "viewport": {"width": self.bcfg.viewport_width, "height": self.bcfg.viewport_height},
            "args": ["--disable-blink-features=AutomationControlled"],
        }
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

    def _connect(self):
        url = self.bcfg.cdp_url
        if not chrome_is_listening(url):
            if not self.bcfg.auto_start_chrome:
                raise BrowserUnavailable(f"Chrome сборщика не запущен (нет ответа на {url}). "
                                         "Запустите его: windows\\start_chrome.bat или `python -m realty_scraper chrome`.")
            start_chrome(self.bcfg)
        try:
            self._browser = self._pw.chromium.connect_over_cdp(url, timeout=15_000)
        except PlaywrightError as e:
            raise BrowserUnavailable(f"Не удалось подключиться к Chrome на {url}: {str(e).splitlines()[0]}") from e
        self.ctx = self._browser.contexts[0] if self._browser.contexts else self._browser.new_context()
        self._new_tab()
        log.info("Подключился к Chrome (%s), работаю в отдельной вкладке", url)

    def _new_tab(self):
        self.page = self.ctx.new_page()
        self.page.set_default_navigation_timeout(self.bcfg.page_timeout_s * 1000)

    def __exit__(self, *exc):
        try:
            if self.connect:
                # закрываем только свою вкладку; окно Chrome остаётся, следующий запуск подключится к нему
                if self.page is not None and not self.page.is_closed():
                    try:
                        self.page.close()
                    except PlaywrightError:
                        pass
            elif self.ctx:
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

    def _ensure_page(self):
        """В режиме connect человек может закрыть вкладку или всё окно — переживаем первое, останавливаемся на втором."""
        if not self.connect:
            return
        if self._browser is not None and not self._browser.is_connected():
            raise BrowserClosed("Окно Chrome сборщика закрыто — сбор остановлен. Запустите его снова.")
        if self.page is None or self.page.is_closed():
            log.warning("Вкладку сборщика закрыли — открываю новую")
            self._new_tab()

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
        self._ensure_page()
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
            if self.connect and self._browser is not None and not self._browser.is_connected():
                raise BrowserClosed("Окно Chrome сборщика закрыто — сбор остановлен. Запустите его снова.") from e
            raise FetchError(f"{type(e).__name__}: {str(e).splitlines()[0][:300]}") from e
        finally:
            self._last_end = time.monotonic()
            self.pages_fetched += 1
        return FetchResult(url=url, final_url=self.page.url, status=resp.status if resp else None,
                           html=html, title=title, fetched_at=fetched_at, elapsed_s=time.monotonic() - t0)

    def current_html(self) -> tuple[str, str]:
        self._ensure_page()
        return self.page.content(), self.page.title()

    def bring_to_front(self):
        try:
            self.page.bring_to_front()
        except PlaywrightError:
            pass

    @property
    def interactive(self) -> bool:
        """Видит ли человек окно браузера (может пройти проверку сам)."""
        return self.connect or not self.bcfg.headless

    def screenshot(self, path: Path):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(path), full_page=False)
        except PlaywrightError:
            pass
