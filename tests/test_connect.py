"""Режим connect: сборщик подключается к отдельному окну Chrome через порт отладки (как на Windows у участника)."""

import os
import shutil
import sqlite3

import pytest
from playwright.sync_api import sync_playwright

from realty_scraper import pipeline
from realty_scraper.browser import chrome_is_listening, find_chrome

from .fakeavito import FakeAvito, listing
from .test_integration import make_cfg

CHROME = os.environ.get("REALTY_TEST_CHROME") or find_chrome()
pytestmark = pytest.mark.skipif(not CHROME, reason="нужен установленный Chrome")
PORT = 9339


@pytest.fixture(autouse=True)
def no_proxy_for_localhost(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


def _close_chrome(cdp_url):
    """Закрыть Chrome, запущенный сборщиком (в обычной работе окно остаётся открытым)."""
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(cdp_url)
        try:
            b.new_browser_cdp_session().send("Browser.close")
        except Exception:  # noqa: BLE001 — соединение рвётся вместе с браузером
            pass


def test_pilot_through_users_chrome(tmp_path):
    items = [listing(8000000001 + i, 3_000_000 + i * 1_000_000, n_photos=2) for i in range(4)]
    cdp = f"http://127.0.0.1:{PORT}"
    assert not chrome_is_listening(cdp)
    with FakeAvito(items, per_page=3) as fake:
        cfg = make_cfg(tmp_path, fake.base_url)
        cfg.browser.mode = "connect"
        cfg.browser.cdp_url = cdp
        cfg.browser.chrome_path = CHROME
        cfg.browser.connect_profile_dir = str(tmp_path / "chrome_profile")
        cfg.browser.chrome_args = ["--headless=new", "--no-sandbox"]  # в тесте без экрана
        cfg.db_file = str(tmp_path / "local" / "realty.sqlite")
        cfg.db_copy_to = str(tmp_path / "drive" / "realty.sqlite")
        try:
            pipeline.run_pilot(cfg, n=4)
            # Chrome был запущен автоматически и остался открытым для следующих запусков
            assert chrome_is_listening(cdp)
            con = sqlite3.connect(cfg.db_path)
            assert con.execute("SELECT COUNT(*) FROM listings WHERE last_parsed_at IS NOT NULL").fetchone()[0] == 4
            assert con.execute("SELECT status FROM runs ORDER BY run_id DESC").fetchone()[0] == "ok"
            con.close()
            # копия базы для команды (папка Google Drive)
            copy = sqlite3.connect(tmp_path / "drive" / "realty.sqlite")
            assert copy.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 4
            copy.close()
            # второй запуск подключается к уже открытому окну, а не запускает новое
            pipeline.run_search(cfg, max_cards=3, sliced=False)
            with sync_playwright() as p:
                b = p.chromium.connect_over_cdp(cdp)
                pages = [pg.url for ctx in b.contexts for pg in ctx.pages]
            assert not any(fake.base_url in u for u in pages)  # свои вкладки сборщик закрыл
        finally:
            if chrome_is_listening(cdp):
                _close_chrome(cdp)
    shutil.rmtree(tmp_path / "chrome_profile", ignore_errors=True)


def test_no_chrome_and_no_autostart_gives_clear_error(tmp_path):
    cfg = make_cfg(tmp_path, "http://127.0.0.1:1")
    cfg.browser.mode = "connect"
    cfg.browser.cdp_url = "http://127.0.0.1:9338"
    cfg.browser.auto_start_chrome = False
    with pytest.raises(Exception, match="не запущен"):
        pipeline.run_probe(cfg)
    con = sqlite3.connect(cfg.db_path)
    assert con.execute("SELECT status FROM runs ORDER BY run_id DESC").fetchone()[0] == "error"
    assert not cfg.db_path.with_suffix(".lock").exists()  # блокировка снята, можно запускать снова
