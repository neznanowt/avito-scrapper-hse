import sqlite3
from datetime import datetime

import pytest

from realty_scraper import db
from realty_scraper.config import Config
from realty_scraper.models import BlockedError, FetchResult
from realty_scraper.platforms.avito.crawler import AvitoCrawler
from realty_scraper.utils import MSK

from .fakeavito import BLOCK_HTML, item_html, listing


class StubSession:
    """Отдаёт заранее заданные страницы вместо браузера."""

    def __init__(self, pages, interactive=False, window_pages=()):
        self.pages = list(pages)
        self.slept = []
        self.interactive = interactive
        self.window_pages = list(window_pages)  # что видно в окне, пока ждём человека
        self.fronted = False

    def fetch(self, url, wait_selector=None):
        html, status, title = self.pages.pop(0)
        return FetchResult(url, url, status, html, title, datetime(2026, 11, 11, 3, 0, tzinfo=MSK), 0.1)

    def current_html(self):
        html, _, title = self.window_pages.pop(0)
        return html, title

    def bring_to_front(self):
        self.fronted = True

    def sleep(self, s):
        self.slept.append(s)

    def screenshot(self, path):
        pass


def _crawler(tmp_path, pages, **session_kw):
    cfg = Config(base_dir=tmp_path)
    cfg.pacing.block_pause_s = 1
    cfg.pacing.max_block_retries = 2
    con = db.connect(cfg.db_path)
    run_id = db.start_run(con, "test", "avito", datetime.now(MSK))
    return AvitoCrawler(cfg, StubSession(pages, **session_kw), con, run_id), con


def test_connect_mode_waits_for_person_to_pass_check(tmp_path, monkeypatch):
    import realty_scraper.platforms.avito.crawler as crawler_mod

    monkeypatch.setattr(crawler_mod.time, "sleep", lambda s: None)
    L = listing(8000000001, 10_000_000)
    blocked = (BLOCK_HTML, 200, "Доступ ограничен: проблема с IP")
    ok = (item_html(L), 200, "ok")
    cr, con = _crawler(tmp_path, [blocked, ok], interactive=True, window_pages=[blocked, ok])
    cr.cfg.browser.mode = "connect"
    res, kind = cr.get("https://www.avito.ru/x_8000000001", "item")
    assert kind == "ok"
    assert cr.session.fronted  # вкладку вывели на передний план, чтобы человек увидел проверку
    assert cr.session.slept == []  # 15-минутной паузы не было: дождались человека


def test_block_then_recover(tmp_path):
    L = listing(8000000001, 10_000_000)
    blocked = (BLOCK_HTML, 200, "Доступ ограничен: проблема с IP")
    cr, con = _crawler(tmp_path, [blocked, (item_html(L), 200, "ok")])
    res, kind = cr.get("https://www.avito.ru/x_8000000001", "item")
    assert kind == "ok" and cr.session.slept == [1]
    assert con.execute("SELECT kind FROM errors").fetchone()[0] == "blocked"


def test_repeated_block_stops_run(tmp_path):
    blocked = (BLOCK_HTML, 403, "Доступ ограничен")
    cr, con = _crawler(tmp_path, [blocked] * 3)
    with pytest.raises(BlockedError):
        cr.get("https://www.avito.ru/x", "search")
    assert con.execute("SELECT COUNT(*) FROM errors WHERE kind='blocked'").fetchone()[0] == 3


def test_reparse_rebuilds_from_raw_pages(tmp_path):
    from realty_scraper import pipeline
    import gzip

    cfg = Config(base_dir=tmp_path)
    L = listing(8000000005, 12_000_000, renovation="евро")
    raw = cfg.path("raw", "avito", "items", "2026-11-11", "8000000005.html.gz")
    raw.parent.mkdir(parents=True)
    with gzip.open(raw, "wt", encoding="utf-8") as f:
        f.write(item_html(L))
    pipeline.run_reparse(cfg)
    con = sqlite3.connect(cfg.db_path)
    row = con.execute("SELECT renovation, price, raw_path FROM listings WHERE listing_id='8000000005'").fetchone()
    assert row[0] == "евро" and row[1] == 12_000_000 and row[2].endswith("8000000005.html.gz")
    # повторный разбор не добавляет наблюдений в панель
    assert con.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
