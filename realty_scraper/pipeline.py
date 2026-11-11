"""Сценарии запуска: пилот, ежедневный сбор, проверка, повторный разбор, экспорт."""

from __future__ import annotations

import csv
import gzip
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

from . import db
from .browser import BrowserSession
from .config import Config
from .models import BlockedError, ParseError
from .photos import download_pending
from .platforms.avito import PLATFORM
from .platforms.avito.crawler import AvitoCrawler
from .platforms.avito.parse_item import parse_item_page
from .platforms.avito.parse_search import parse_search_page
from .platforms.avito.urls import search_url
from .report import pilot_report
from .utils import MSK, RunLock, now_msk

log = logging.getLogger("realty")


def setup_logging(log_dir: Path, mode: str):
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("realty")
    logger.setLevel(logging.INFO)
    for h in list(logger.handlers):
        logger.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fmt.converter = lambda t: datetime.fromtimestamp(t, MSK).timetuple()  # журнал по московскому времени
    fh = logging.FileHandler(log_dir / f"{now_msk():%Y-%m-%d}_{mode}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)


class Runner:
    """Один запуск: блокировка, журнал, запись в runs, браузер, итоговый статус."""

    def __init__(self, cfg: Config, mode: str, need_browser: bool = True, budget_hours: float | None = None):
        self.cfg, self.mode, self.need_browser = cfg, mode, need_browser
        self.deadline = time.monotonic() + budget_hours * 3600 if budget_hours else None
        self.stats: dict = {}
        self.status = "ok"
        self.search_complete = None
        self.session = None

    def __enter__(self):
        self.lock = RunLock(self.cfg.path(".lock")).__enter__()
        setup_logging(self.cfg.path("logs"), self.mode)
        self.con = db.connect(self.cfg.db_path)
        self.started = now_msk()
        self.run_id = db.start_run(self.con, self.mode, PLATFORM, self.started)
        log.info("Запуск №%d (%s), данные: %s", self.run_id, self.mode, self.cfg.data_path)
        if self.need_browser:
            self.session = BrowserSession(self.cfg.browser, self.cfg.pacing,
                                          self.cfg.path(self.cfg.browser.user_data_dir), self.deadline).__enter__()
        self.crawler = AvitoCrawler(self.cfg, self.session, self.con, self.run_id, self.deadline)
        return self

    def close_browser(self):
        if self.session:
            self.session.__exit__(None, None, None)
            self.session = None

    def __exit__(self, et, ev, tb):
        if et is None:
            status = self.status
        elif issubclass(et, BlockedError):
            status = "blocked"
            log.error("Остановлено из-за блокировки: %s", ev)
        elif issubclass(et, KeyboardInterrupt):
            status = "interrupted"
            log.warning("Прервано вручную")
        else:
            status = "error"
            log.exception("Запуск упал")
        try:
            self.close_browser()
        finally:
            db.finish_run(self.con, self.run_id, status, now_msk(), self.stats, self.search_complete)
            log.info("Запуск №%d завершён: %s; %s", self.run_id, status,
                     json.dumps(self.stats, ensure_ascii=False, default=str))
            self.con.close()
            self.lock.__exit__(None, None, None)
        self.final_status = status
        return False


def _partial(*stage_stats) -> bool:
    return any(s.get("stopped_by_deadline") or s.get("stopped") == "DeadlineReached" for s in stage_stats)


def run_pilot(cfg: Config, n: int = 500, photos: bool = True) -> Path:
    with Runner(cfg, "pilot") as r:
        r.stats["search"] = r.crawler.crawl_search(max_cards=n, sliced=False)
        r.stats["items"] = r.crawler.crawl_items("run", limit=n)
        r.close_browser()
        if photos:
            r.stats["photos"] = download_pending(r.con, cfg, PLATFORM, r.run_id, deadline=r.deadline)
        text = pilot_report(r.con, r.run_id, PLATFORM, r.stats, r.crawler.timings, r.started)
        out = cfg.path("reports", f"pilot_{r.started:%Y%m%d_%H%M}.md")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        log.info("Отчёт по пилоту: %s", out)
        print(text)
    return out


def run_daily(cfg: Config, budget_hours: float | None = 7.0, photos: bool = True):
    a = cfg.avito
    with Runner(cfg, "daily", budget_hours=budget_hours) as r:
        s = r.crawler.crawl_search(sliced=True)
        r.stats["search"] = s
        r.search_complete = bool(s.get("complete"))
        if r.search_complete:
            started = datetime.fromisoformat(s["started_at"])
            s["marked_missing"] = db.mark_missing(r.con, PLATFORM, started, now_msk())
        else:
            log.warning("Выдача обойдена не полностью — пропавшие объявления не помечаются")
        for mode, lim in (("new", a.max_new_items_per_run), ("verify", a.max_verify_per_run),
                          ("refresh", a.max_refresh_per_run)):
            st = r.crawler.crawl_items(mode, lim)
            r.stats[f"items_{mode}"] = st
            if st.get("stopped_by_deadline"):
                break
        r.close_browser()
        if photos:
            r.stats["photos"] = download_pending(r.con, cfg, PLATFORM, r.run_id, deadline=r.deadline)
        if _partial(*r.stats.values()):
            r.status = "partial"


def run_search(cfg: Config, max_cards: int | None, sliced: bool):
    with Runner(cfg, "search") as r:
        s = r.crawler.crawl_search(max_cards=max_cards, sliced=sliced)
        r.stats["search"] = s
        r.search_complete = bool(s.get("complete"))


def run_items(cfg: Config, mode: str, limit: int | None):
    with Runner(cfg, f"items_{mode}") as r:
        r.stats["items"] = r.crawler.crawl_items(mode, limit)


def run_photos(cfg: Config, limit: int | None):
    with Runner(cfg, "photos", need_browser=False) as r:
        r.stats["photos"] = download_pending(r.con, cfg, PLATFORM, r.run_id, limit=limit)


def run_probe(cfg: Config):
    """Быстрая проверка на живом сайте: 3 страницы, ничего не пишет в таблицу объявлений."""
    a = cfg.avito
    with Runner(cfg, "probe") as r:
        res, kind = r.crawler.get(search_url(a), "search")
        print(f"Выдача: {kind}, HTTP {res.status if res else '—'}, «{res.title if res else ''}»")
        if kind != "ok":
            return
        sp = parse_search_page(res.html, a.base_url, a.region_slug)
        print(f"  объявлений в запросе: {sp.total_count}; карточек на странице: {len(sp.cards)}; "
              f"чужих отброшено: {sp.foreign_skipped}; следующая страница: {sp.has_next}")
        for c in sp.cards[:3]:
            print(f"  · {c.listing_id} | {c.price} ₽ | {c.title} | {c.date_label}")
        res2, kind2 = r.crawler.get(search_url(a, pmax=5_000_000), "search")
        if kind2 == "ok":
            sp2 = parse_search_page(res2.html, a.base_url, a.region_slug)
            ok = sp2.total_count is not None and sp.total_count and sp2.total_count < sp.total_count
            print(f"Фильтр по цене (до 5 млн): {sp2.total_count} объявлений — "
                  f"{'работает, нарезка по цене возможна' if ok else 'НЕ РАБОТАЕТ, проверьте параметры pmin/pmax'}")
        if not sp.cards:
            return
        res3, kind3 = r.crawler.get(sp.cards[0].url, "item")
        print(f"Карточка: {kind3}, HTTP {res3.status if res3 else '—'}")
        if kind3 != "ok":
            return
        try:
            item = parse_item_page(res3.html, res3.fetched_at, a.base_url, cfg.seller_hash_salt, a.image_sizes)
        except ParseError as e:
            print(f"  ОШИБКА разбора: {e}")
            return
        L = item.listing
        print(f"  источник: {item.source}; статус: {item.status}; фото: {L.get('photo_count')}")
        for k in ("listing_id", "title", "price", "area_total", "rooms", "floor", "floors_total", "renovation",
                  "house_type", "build_year", "address", "lat", "lon", "metro_name", "seller_type",
                  "views_total", "date_label", "house_id"):
            print(f"  {k:15} {L.get(k)}")
        empty = [k for k, v in L.items() if v in (None, "") and not k.endswith("_json")]
        print(f"  пустые поля: {', '.join(sorted(empty)) or 'нет'}")


def run_reparse(cfg: Config):
    """Повторно разбирает сохранённые страницы карточек (после правки парсера), без обращения к сайту."""
    with Runner(cfg, "reparse", need_browser=False) as r:
        files = sorted(cfg.path("raw", PLATFORM, "items").glob("*/*.html.gz"))
        latest: dict[str, Path] = {}
        for f in files:  # папки по датам: более поздний файл перезаписывает ранний
            latest[f.name.split(".")[0]] = f
        st = {"files": len(latest), "parsed": 0, "errors": 0}
        for f in latest.values():
            fetched_at = datetime.fromtimestamp(f.stat().st_mtime, MSK)
            try:
                with gzip.open(f, "rt", encoding="utf-8") as fh:
                    item = parse_item_page(fh.read(), fetched_at, cfg.avito.base_url,
                                           cfg.seller_hash_salt, cfg.avito.image_sizes)
            except (ParseError, OSError) as e:
                db.log_error(r.con, r.run_id, PLATFORM, "reparse", str(f), "parse_error", str(e), now_msk())
                st["errors"] += 1
                continue
            db.upsert_item(r.con, PLATFORM, item, r.run_id, fetched_at,
                           str(f.relative_to(cfg.data_path)), record_observation=False)
            st["parsed"] += 1
        r.stats["reparse"] = st
        print(st)


def print_stats(cfg: Config):
    con = db.connect(cfg.db_path)
    q = lambda sql, *a: con.execute(sql, a).fetchall()  # noqa: E731
    print(f"База: {cfg.db_path}")
    for row in q("SELECT platform, status, COUNT(*) n, SUM(last_parsed_at IS NOT NULL) parsed "
                 "FROM listings GROUP BY platform, status"):
        print(f"  {row['platform']:6} {row['status'] or '—':8} объявлений {row['n']:7}, разобрано {row['parsed']}")
    ph = q("SELECT COUNT(*) n, SUM(downloaded_at IS NOT NULL) ok, SUM(downloaded_at IS NULL AND attempts>0) bad, "
           "COALESCE(SUM(bytes),0) b FROM photos WHERE is_current=1")[0]
    print(f"  фото: всего {ph['n']}, скачано {ph['ok'] or 0}, с ошибкой {ph['bad'] or 0}, {ph['b'] / 2**30:.2f} ГБ")
    print("Последние запуски:")
    for row in q("SELECT run_id, mode, started_at, finished_at, status FROM runs ORDER BY run_id DESC LIMIT 7"):
        print(f"  №{row['run_id']:<4} {row['mode']:13} {row['started_at']} → {row['finished_at'] or '…'}  {row['status']}")
    errs = q("SELECT kind, COUNT(*) n FROM errors WHERE ts >= datetime('now','-2 day') GROUP BY kind ORDER BY n DESC")
    if errs:
        print("Ошибки за 2 дня: " + ", ".join(f"{e['kind']}={e['n']}" for e in errs))
    con.close()


def backup_db(cfg: Config, keep: int = 14) -> Path:
    """Согласованная копия базы (SQLite backup API работает и во время записи). Хранит последние keep копий."""
    dest_dir = cfg.path("backup")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"realty_{now_msk():%Y%m%d_%H%M}.sqlite"
    src = db.connect(cfg.db_path)
    out = sqlite3.connect(dest)
    with out:
        src.backup(out)
    out.close()
    src.close()
    for old in sorted(dest_dir.glob("realty_*.sqlite"))[:-keep]:
        old.unlink()
    print(f"Резервная копия: {dest}")
    return dest


def export_csv(cfg: Config, out_dir: Path):
    """Выгрузка для коллег: listings.csv и photos.csv (UTF-8 с BOM — открывается в Excel)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    con = db.connect(cfg.db_path)
    for table, name in (("listings", "listings.csv"), ("photos", "photos.csv")):
        cur = con.execute(f"SELECT * FROM {table}" + (" WHERE is_current=1" if table == "photos" else ""))
        with open(out_dir / name, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow([d[0] for d in cur.description])
            n = 0
            for row in cur:
                w.writerow(list(row))
                n += 1
        print(f"{out_dir / name}: {n} строк")
    con.close()
