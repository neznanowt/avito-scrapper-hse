"""Сквозной тест на локальном «фальшивом Авито» с настоящим браузером (Playwright + Chrome).

Проверяет: обход выдачи с нарезкой по цене и пагинацией, отсев чужого региона, разбор карточек,
404 → removed, скачивание и уменьшение фото, pHash, пометку пропавших объявлений и отчёт пилота.
"""

import os
import shutil
import sqlite3

import pytest

from realty_scraper import pipeline
from realty_scraper.config import Config

from .fakeavito import SEARCH_PATH, FakeAvito, listing

pytestmark = pytest.mark.skipif(not (shutil.which("google-chrome") or os.environ.get("REALTY_TEST_CHROMIUM")),
                                reason="нужен Chrome для Playwright")


def make_cfg(tmp_path, base_url) -> Config:
    cfg = Config(base_dir=tmp_path)
    cfg.data_dir = "data"
    cfg.browser.headless = True
    cfg.browser.channel = None if os.environ.get("REALTY_TEST_CHROMIUM") else "chrome"
    cfg.pacing.delay_min_s = cfg.pacing.delay_max_s = 0
    cfg.pacing.long_pause_every = 0
    cfg.photos.delay_min_s = cfg.photos.delay_max_s = 0
    cfg.avito.base_url = base_url
    cfg.avito.search_path = SEARCH_PATH
    cfg.avito.slice_threshold = 4  # чтобы нарезка по цене включилась на 9 объявлениях
    cfg.avito.min_slice_width = 1000
    return cfg


@pytest.fixture(autouse=True)
def no_proxy_for_localhost(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


def test_pilot_then_daily(tmp_path):
    prices = [3_100_000, 4_500_000, 6_200_000, 7_900_000, 9_800_000, 12_500_000, 18_000_000, 26_000_000, 41_000_000]
    items = [listing(8000000001 + i, p, n_photos=2 + i % 3) for i, p in enumerate(prices)]
    foreign = [listing(8100000001, 2_900_000)]
    with FakeAvito(items, per_page=3, missing_items={8000000009}, foreign=foreign) as fake:
        cfg = make_cfg(tmp_path, fake.base_url)

        # --- пилот на 5 объявлениях ---
        report_path = pipeline.run_pilot(cfg, n=5)
        con = sqlite3.connect(cfg.db_path)
        con.row_factory = sqlite3.Row
        parsed = con.execute("SELECT * FROM listings WHERE last_parsed_at IS NOT NULL").fetchall()
        assert 5 <= len(parsed) <= 6  # стр. 1 (3) + стр. 2 (3), остановка после 5-й уникальной
        assert all(r["renovation"] == "косметический" and r["lat"] for r in parsed)
        assert con.execute("SELECT COUNT(*) FROM listings WHERE listing_id='8100000001'").fetchone()[0] == 0
        ph = con.execute("SELECT * FROM photos WHERE downloaded_at IS NOT NULL").fetchall()
        assert ph and all(max(r["width"], r["height"]) == 384 and len(r["phash"]) == 16 for r in ph)
        assert all((cfg.data_path / r["local_path"]).exists() for r in ph)
        assert "Заполненность полей" in report_path.read_text(encoding="utf-8")
        assert con.execute("SELECT status FROM runs ORDER BY run_id DESC").fetchone()[0] == "ok"

        # --- ежедневный запуск: вся выдача с нарезкой по цене ---
        pipeline.run_daily(cfg, budget_hours=None)
        run = con.execute("SELECT * FROM runs ORDER BY run_id DESC").fetchone()
        assert run["status"] == "ok" and run["search_complete"] == 1
        assert con.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 9
        assert any("pmax=" in r for r in fake.requests)  # нарезка действительно была
        slices = con.execute("SELECT * FROM search_slices WHERE run_id=?", (run["run_id"],)).fetchall()
        assert len(slices) >= 2 and all(s["total_count"] <= 4 or s["complete"] for s in slices)
        removed = con.execute("SELECT status FROM listings WHERE listing_id='8000000009'").fetchone()[0]
        assert removed == "removed"  # карточка отдаёт 404
        assert con.execute("SELECT COUNT(*) FROM listings WHERE last_parsed_at IS NULL").fetchone()[0] == 1

        # --- объявление пропало из выдачи → помечается, проверяется и остаётся активным (карточка жива) ---
        fake.listings.pop(0)
        pipeline.run_daily(cfg, budget_hours=None)
        row = con.execute("SELECT missing_since, status, status_checked_at FROM listings "
                          "WHERE listing_id='8000000001'").fetchone()
        assert row["missing_since"] and row["status"] == "active" and row["status_checked_at"]
        obs = con.execute("SELECT COUNT(DISTINCT obs_date) FROM observations").fetchone()[0]
        assert obs >= 1
        con.close()
