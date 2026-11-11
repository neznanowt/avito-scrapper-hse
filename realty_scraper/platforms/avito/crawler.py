"""Обход Авито: выдача (с нарезкой по цене) и карточки. Блокировки распознаются и останавливают запуск."""

from __future__ import annotations

import gzip
import logging
import math
import time
from collections import Counter
from pathlib import Path

from ... import db
from ...browser import BrowserSession, FetchError
from ...config import Config
from ...models import BlockedError, DeadlineReached, FetchResult, ParseError, SearchPage
from ...utils import now_msk
from . import PLATFORM
from .parse_item import HYDRATION_MARK, parse_item_page
from .parse_search import EMPTY_MARKERS, parse_search_page
from .urls import search_url

log = logging.getLogger("realty")

BLOCK_PHRASES = (
    "доступ ограничен", "доступ временно ограничен", "проблема с ip",
    "подтвердите, что запросы отправляли вы", "вы робот", "captcha-container",
)


def classify_page(res: FetchResult, expect: str) -> str:
    """ok | empty | not_found | blocked | unexpected."""
    html = res.html
    if expect == "item" and res.status in (404, 410):
        return "not_found"
    if expect == "item":
        has_content = HYDRATION_MARK in html or 'data-marker="item-view/' in html
    else:
        has_content = 'data-marker="item"' in html or 'data-marker="page-title/count"' in html
    if has_content and (res.status or 200) < 400:
        return "ok"
    low = (res.title or "").lower() + " " + html[:300_000].lower()
    if res.status in (403, 429) or any(p in low for p in BLOCK_PHRASES):
        return "blocked"
    if expect == "search" and any(p in low for p in EMPTY_MARKERS):
        return "empty"
    return "unexpected"


class StopCrawl(Exception):
    pass


class AvitoCrawler:
    platform = PLATFORM

    def __init__(self, cfg: Config, session: BrowserSession, con, run_id: int, deadline: float | None = None):
        self.cfg = cfg
        self.acfg = cfg.avito
        self.session = session
        self.con = con
        self.run_id = run_id
        self.deadline = deadline
        self.timings = {"search": [], "item": []}

    # ---------- загрузка с проверкой блокировок ----------
    def _err(self, stage, url, kind, msg):
        log.warning("[%s] %s: %s — %s", stage, kind, url, msg)
        db.log_error(self.con, self.run_id, PLATFORM, stage, url, kind, msg, now_msk())

    def _debug_dump(self, res: FetchResult, tag: str) -> Path:
        p = self.cfg.path("debug", f"{now_msk():%Y%m%d_%H%M%S}_{tag}.html.gz")
        p.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(p, "wt", encoding="utf-8") as f:
            f.write(res.html)
        return p

    def get(self, url: str, expect: str) -> tuple[FetchResult | None, str]:
        pc = self.cfg.pacing
        blocks = 0
        wait_sel = '[data-marker="item"]' if expect == "search" else None
        while True:
            t0 = time.monotonic()
            try:
                res = self.session.fetch(url, wait_selector=wait_sel)
            except FetchError as e:
                self._err(expect, url, "fetch_error", str(e))
                return None, "fetch_error"
            self.timings[expect].append(time.monotonic() - t0)
            kind = classify_page(res, expect)
            if kind in ("ok", "empty", "not_found"):
                return res, kind
            if kind == "unexpected":
                dump = self._debug_dump(res, f"unexpected_{expect}")
                self._err(expect, url, "unexpected_page", f"status={res.status} title={res.title!r} saved={dump.name}")
                return res, kind
            # блокировка
            blocks += 1
            shot = self.cfg.path("debug", f"{now_msk():%Y%m%d_%H%M%S}_blocked.png")
            self.session.screenshot(shot)
            self._err(expect, url, "blocked", f"status={res.status} title={res.title!r} screenshot={shot.name}")
            if pc.wait_for_operator and not self.cfg.browser.headless and self._wait_for_operator(expect):
                continue
            if blocks > pc.max_block_retries:
                raise BlockedError(f"Авито блокирует запросы ({blocks} раз подряд): {res.title!r}")
            log.warning("Пауза %.0f с после блокировки", pc.block_pause_s)
            self.session.sleep(pc.block_pause_s)

    def _wait_for_operator(self, expect: str) -> bool:
        log.warning("Ожидаю, пока оператор пройдёт проверку в окне браузера (до %.0f с)…",
                    self.cfg.pacing.operator_wait_max_s)
        end = time.monotonic() + self.cfg.pacing.operator_wait_max_s
        while time.monotonic() < end:
            time.sleep(10)
            html, title = self.session.current_html()
            fake = FetchResult("", "", 200, html, title, now_msk(), 0)
            if classify_page(fake, expect) == "ok":
                log.info("Проверка пройдена, продолжаю")
                return True
        return False

    def _check_deadline(self):
        if self.deadline is not None and time.monotonic() > self.deadline:
            raise DeadlineReached()

    # ---------- выдача ----------
    def crawl_search(self, max_cards: int | None = None, sliced: bool = True) -> dict:
        """Обходит выдачу. Если в запросе больше slice_threshold объявлений, делит ценовой диапазон пополам
        (по геометрическому среднему), пока каждая часть не уместится в 100 страниц."""
        st = Counter()
        self._seen: set[str] = set()
        self._max_cards = max_cards
        self._complete = True
        self._started = now_msk()
        a = self.acfg
        try:
            lo, hi = (a.price_min, a.price_max) if sliced else (None, None)
            first = self._search_page(lo, hi, 1, st)
            if first is not None:
                st["total_in_query"] = first.total_count or 0
                self._crawl_range(lo, hi, first, st, depth=0, sliced=sliced)
            else:
                self._complete = False
        except (StopCrawl, DeadlineReached) as e:
            self._complete = False
            st["stopped"] = type(e).__name__
        st["unique_cards"] = len(self._seen)
        st["complete"] = self._complete and max_cards is None
        st["started_at"] = self._started.isoformat(timespec="seconds")
        return dict(st)

    def _search_page(self, lo, hi, page, st):
        self._check_deadline()
        url = search_url(self.acfg, page=page, pmin=lo, pmax=hi)
        res, kind = self.get(url, "search")
        st["search_pages"] += 1
        if kind == "empty":
            return SearchPage(total_count=0, cards=[], has_next=False, empty=True)
        if kind != "ok":
            self._complete = False
            st["search_failed"] += 1
            return None
        if self.acfg.save_raw_search:
            p = self.cfg.path("raw", PLATFORM, "search", f"{res.fetched_at:%Y-%m-%d}",
                              f"{res.fetched_at:%H%M%S}_p{page}_{lo}_{hi}.html.gz")
            p.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(p, "wt", encoding="utf-8") as f:
                f.write(res.html)
        sp = parse_search_page(res.html, self.acfg.base_url, self.acfg.region_slug)
        st["foreign_skipped"] += sp.foreign_skipped
        if not sp.cards and not sp.empty:
            self._err("search", url, "no_cards", f"total={sp.total_count}")
        per_page = 60
        for i, card in enumerate(sp.cards):
            db.upsert_search_card(self.con, PLATFORM, card, self.run_id, res.fetched_at, (page - 1) * per_page + i)
            if card.listing_id not in self._seen:
                self._seen.add(card.listing_id)
                st["cards"] += 1
        self.con.commit()
        rng = "без фильтра цены" if lo is None and hi is None else f"{lo or 0:,}–{hi:,} ₽".replace(",", " ")
        log.info("Выдача [%s] стр.%d: %d карточек (всего в запросе %s, уникальных %d)",
                 rng, page, len(sp.cards), sp.total_count, len(self._seen))
        if self._max_cards and len(self._seen) >= self._max_cards:
            raise StopCrawl()
        return sp

    def _crawl_range(self, lo, hi, first, st, depth: int, sliced: bool):
        a = self.acfg
        total = first.total_count or 0
        if sliced and total > a.slice_threshold and hi - lo > a.min_slice_width and depth < 24:
            mid = self._split(lo, hi)
            left = self._search_page(lo, mid, 1, st)
            right = self._search_page(mid + 1, hi, 1, st)
            if left is None or right is None:
                self._complete = False
            elif (left.total_count or 0) + (right.total_count or 0) > 1.5 * total + 50:
                self._err("search", search_url(a, 1, lo, mid), "price_filter_ignored",
                          f"parent={total} left={left.total_count} right={right.total_count}")
                self._complete = False
                self._crawl_pages(lo, hi, first, st)
                return
            if left is not None:
                self._crawl_range(lo, mid, left, st, depth + 1, sliced)
            if right is not None:
                self._crawl_range(mid + 1, hi, right, st, depth + 1, sliced)
            return
        self._crawl_pages(lo, hi, first, st)

    def _crawl_pages(self, lo, hi, first, st):
        a = self.acfg
        total = first.total_count or 0
        per_page = max(len(first.cards), 1)
        need = min(a.max_pages_per_query, math.ceil(total / per_page)) if total else 1
        pages, cards, has_next = 1, len(first.cards), first.has_next
        if total > a.max_pages_per_query * per_page:
            self._complete = False
            self._err("search", search_url(a, 1, lo, hi), "slice_too_big",
                      f"{total} объявлений не помещаются в {a.max_pages_per_query} страниц")
        page = 2
        while page <= need and has_next:
            sp = self._search_page(lo, hi, page, st)
            if sp is None:
                break
            pages += 1
            cards += len(sp.cards)
            if not sp.cards:
                break
            has_next = sp.has_next
            page += 1
        db.log_slice(self.con, self.run_id, PLATFORM, lo, hi, total, pages, cards, complete=pages >= need)

    @staticmethod
    def _split(lo: int, hi: int) -> int:
        base = max(lo, 1_000_000)
        mid = int(math.sqrt(base * hi)) if hi > base else (lo + hi) // 2
        if not lo < mid < hi:
            mid = (lo + hi) // 2
        return int(round(mid, -4)) if hi - lo > 100_000 else mid

    # ---------- карточки ----------
    def crawl_items(self, mode: str, limit: int | None = None) -> dict:
        now = now_msk()
        rows = db.select_items(self.con, PLATFORM, mode, limit, now, self.acfg.refresh_after_days, self.run_id)
        st = Counter(selected=len(rows))
        log.info("Карточки (%s): к обходу %d", mode, len(rows))
        for row in rows:
            try:
                self._check_deadline()
                res, kind = self.get(row["url"], "item")
            except DeadlineReached:
                st["stopped_by_deadline"] = 1
                break
            if kind == "not_found":
                db.set_status(self.con, PLATFORM, row["listing_id"], "removed", res.fetched_at, f"http {res.status}")
                st["removed"] += 1
                continue
            if kind != "ok":
                st["failed"] += 1
                continue
            try:
                item = parse_item_page(res.html, res.fetched_at, self.acfg.base_url,
                                       self.cfg.seller_hash_salt, self.acfg.image_sizes)
            except ParseError as e:
                dump = self._debug_dump(res, f"parse_{row['listing_id']}")
                self._err("item", row["url"], "parse_error", f"{e}; saved={dump.name}")
                st["parse_error"] += 1
                continue
            if item.listing["listing_id"] != str(row["listing_id"]):
                self._err("item", row["url"], "id_mismatch",
                          f"ожидали {row['listing_id']}, получили {item.listing['listing_id']}")
            raw_path = self._save_raw(res, item.listing["listing_id"]) if self.acfg.save_raw_items else None
            db.upsert_item(self.con, PLATFORM, item, self.run_id, res.fetched_at, raw_path)
            st["parsed"] += 1
            st[f"status_{item.status}"] += 1
            if item.source != "hydration":
                st["dom_fallback"] += 1
            if st["parsed"] % 25 == 0:
                log.info("Карточки (%s): %d/%d", mode, st["parsed"], len(rows))
        return dict(st)

    def _save_raw(self, res: FetchResult, listing_id: str) -> str:
        p = self.cfg.path("raw", PLATFORM, "items", f"{res.fetched_at:%Y-%m-%d}", f"{listing_id}.html.gz")
        p.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(p, "wt", encoding="utf-8") as f:
            f.write(res.html)
        return str(p.relative_to(self.cfg.data_path))
