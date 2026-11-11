"""Разбор страницы выдачи Авито.

Структура (проверено 11.11.2026): встроенного JSON с каталогом нет, карточки — в HTML
`[data-marker="item"][data-item-id]`; запасной источник — JSON-LD (schema.org AggregateOffer),
где у каждого объявления есть url, price, name. Число объявлений — `[data-marker="page-title/count"]`.
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ...models import SearchCard, SearchPage
from ...utils import clean_text, parse_int
from .urls import absolute, id_from_url

EMPTY_MARKERS = ("ничего не найдено", "объявлений не найдено")


def _card_price(el) -> int | None:
    meta = el.select_one('meta[itemprop="price"]')
    if meta and meta.get("content"):
        v = parse_int(meta["content"])
        if v:
            return v
    for marker in ("item-price-value", "item-price"):
        node = el.select_one(f'[data-marker="{marker}"]')
        if node:
            v = parse_int(node.get("content") or node.get_text(" "))
            if v:
                return v
    return None


def _jsonld_offers(soup) -> tuple[list[dict], int | None]:
    offers, total = [], None
    for s in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(s.string or s.get_text() or "")
        except (ValueError, TypeError):
            continue
        nodes = data.get("@graph", [data]) if isinstance(data, dict) else data
        for node in nodes if isinstance(nodes, list) else []:
            agg = node.get("offers") if isinstance(node, dict) else None
            if isinstance(agg, dict) and agg.get("@type") == "AggregateOffer":
                total = total or parse_int(agg.get("offerCount"))
                offers.extend(o for o in agg.get("offers") or [] if isinstance(o, dict))
    return offers, total


def parse_search_page(html: str, base_url: str, region_slug: str) -> SearchPage:
    soup = BeautifulSoup(html, "lxml")
    prefix = f"/{region_slug}/kvartiry/"

    count_node = soup.select_one('[data-marker="page-title/count"]')
    total = parse_int(count_node.get_text()) if count_node else None

    cards: list[SearchCard] = []
    seen: set[str] = set()
    foreign = 0
    for el in soup.select('[data-marker="item"][data-item-id]'):
        lid = el.get("data-item-id")
        a = (el.select_one('a[data-marker="item-title"]') or el.select_one('a[itemprop="url"]')
             or el.select_one("a[href]"))
        href = a.get("href") if a else None
        if not href or not urlsplit(href).path.startswith(prefix):
            foreign += 1
            continue
        if lid in seen:
            continue
        seen.add(lid)
        title_node = el.select_one('[itemprop="name"]') or a
        date_node = el.select_one('[data-marker="item-date"]')
        addr_node = el.select_one('[data-marker="item-address"]')
        cards.append(SearchCard(
            listing_id=lid,
            url=absolute(base_url, href),
            title=clean_text(title_node.get_text(" ")) if title_node else None,
            price=_card_price(el),
            date_label=clean_text(date_node.get_text(" ")) if date_node else None,
            address_raw=clean_text(addr_node.get_text(" ")) if addr_node else None,
        ))

    offers, ld_total = _jsonld_offers(soup)
    by_id = {}
    for o in offers:
        url = o.get("url") or ""
        lid = id_from_url(url)
        if lid and urlsplit(url).path.startswith(prefix):
            by_id[lid] = o
    if cards:
        for c in cards:  # дополняем пропуски из JSON-LD
            o = by_id.get(c.listing_id)
            if o:
                c.price = c.price or parse_int(o.get("price"))
                c.title = c.title or clean_text(o.get("name"))
    else:  # вёрстка поменялась — работаем по JSON-LD
        for lid, o in by_id.items():
            cards.append(SearchCard(listing_id=lid, url=absolute(base_url, o["url"]),
                                    title=clean_text(o.get("name")), price=parse_int(o.get("price"))))

    nxt = soup.select_one('[data-marker="pagination-button/nextPage"]')
    has_next = bool(nxt) and not nxt.has_attr("disabled") and nxt.get("aria-disabled") != "true"
    text_low = soup.get_text(" ").lower() if not cards else ""
    empty = not cards and any(m in text_low for m in EMPTY_MARKERS)
    if total is None:
        total = ld_total if ld_total is not None else (0 if empty else None)
    return SearchPage(total_count=total, cards=cards, has_next=has_next, foreign_skipped=foreign, empty=empty)
