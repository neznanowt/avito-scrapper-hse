from datetime import datetime

import pytest

from realty_scraper.models import FetchResult, ParseError
from realty_scraper.platforms.avito.crawler import AvitoCrawler, classify_page
from realty_scraper.platforms.avito.parse_item import parse_item_page
from realty_scraper.platforms.avito.parse_search import parse_search_page
from realty_scraper.utils import MSK

from .fakeavito import BLOCK_HTML, item_dom_only_html, item_html, listing, search_html

NOW = datetime(2026, 11, 11, 15, 0, tzinfo=MSK)
BASE = "https://www.avito.ru"


def test_item_from_hydration():
    L = listing(8380391827, 23_500_000, area=50.0, n_photos=9)
    item = parse_item_page(item_html(L), NOW, BASE, salt="x")
    d = item.listing
    assert item.source == "hydration" and item.status == "active"
    assert d["listing_id"] == "8380391827"
    assert d["url"] == BASE + "/sankt-peterburg/kvartiry/2-k._kvartira_50_m_8380391827"
    assert d["price"] == 23_500_000 and d["area_total"] == 50.0 and d["price_per_m2"] == 470_000
    assert (d["rooms"], d["floor"], d["floors_total"]) == (2, 1, 7)
    assert (d["area_kitchen"], d["area_living"], d["ceiling_height"]) == (8.0, 32.0, 3.0)
    assert d["renovation"] == "косметический" and d["bathroom"] == "раздельный"
    assert d["house_type"] == "кирпичный" and d["build_year"] == 1955 and d["passenger_lifts"] == 1
    assert (d["lat"], d["lon"]) == (59.954529, 30.32698)  # из geo, а не центр города из location
    assert d["metro_name"] == "Горьковская" and (d["metro_walk_min"], d["metro_walk_max"]) == (6, 10)
    assert d["metro_distance_m"] == 500 and len(d["metro_json"]) == 2
    assert d["seller_type"] == "Частное лицо" and d["is_company"] is False and d["seller_key"]
    assert d["house_id"] == "22500" and d["views_total"] == 120 and d["views_today"] == 3
    assert d["date_label_at"].startswith("2026-11-11T14:04")
    assert d["params_json"]["Стоимость ремонта"].startswith("от 643")
    assert d["is_apartments"] is False and d["is_share"] is False and d["is_secondary"] is True
    assert d["photo_count"] == 9 and len(item.photos) == 9
    assert item.photos[0].url.endswith("_0_640.jpg") and item.photos[0].url_large.endswith("_0_1280.jpg")
    assert "Продаётся уютная сталинка" in d["description"]


def test_item_closed_studio_and_apartments():
    L = listing(1, 5_000_000, area=25.0, rooms="студия", closed=True, title="Квартира-студия, 25 м², 3/9 эт.")
    item = parse_item_page(item_html(L), NOW, BASE)
    assert item.status == "closed" and item.listing["status_detail"] == "Снято с публикации"
    assert item.listing["is_studio"] is True and item.listing["rooms"] == 0
    L2 = listing(2, 9_000_000, title="Апартаменты-студия, 30 м², 5/12 эт.")
    assert parse_item_page(item_html(L2), NOW, BASE).listing["is_apartments"] is True
    L3 = listing(3, 2_000_000, title="Доля в 3-к. квартире, 70 м², 2/5 эт.")
    assert parse_item_page(item_html(L3), NOW, BASE).listing["is_share"] is True


def test_item_dom_fallback():
    L = listing(4567890123, 7_200_000, area=31.5, renovation="требует ремонта")
    item = parse_item_page(item_dom_only_html(L), NOW, BASE)
    d = item.listing
    assert item.source == "dom"
    assert d["listing_id"] == "4567890123" and d["price"] == 7_200_000 and d["area_total"] == 31.5
    assert d["renovation"] == "требует ремонта" and d["balcony"] == "балкон" and d["bathroom"] == "совмещенный"
    assert d["house_type"] == "панельный" and d["build_year"] == 1968 and d["floors_total"] == 7
    assert (d["lat"], d["lon"]) == (59.9, 30.3) and d["views_total"] == 1205 and d["views_today"] == 7
    assert d["seller_type"] == "Агентство" and d["date_label_at"].startswith("2026-11-10T09:15")
    assert len(item.photos) == 2


def test_item_garbage_raises():
    with pytest.raises(ParseError):
        parse_item_page("<html><body>Hello</body></html>", NOW, BASE)


def test_search_page():
    cards = [listing(100 + i, 10_000_000 + i) for i in range(3)]
    foreign = [listing(999, 3_000_000)]
    sp = parse_search_page(search_html(cards, 19246, True, foreign), BASE, "sankt-peterburg")
    assert sp.total_count == 19246 and sp.has_next and sp.foreign_skipped == 1
    assert [c.listing_id for c in sp.cards] == ["100", "101", "102"]
    c = sp.cards[0]
    assert c.price == 10_000_000 and c.title.startswith("2-к. квартира")
    assert c.url == BASE + "/sankt-peterburg/kvartiry/2-k._kvartira_50_m_100"  # без ?context=
    assert "Куйбышева" in c.address_raw


def test_search_jsonld_fallback_when_markup_changes():
    cards = [listing(8589793885 + i, 5_000_000) for i in range(2)]
    html = search_html(cards, 2, False).replace('data-marker="item"', 'data-marker="card-v2"')
    sp = parse_search_page(html, BASE, "sankt-peterburg")
    assert [c.listing_id for c in sp.cards] == ["8589793885", "8589793886"] and sp.cards[0].price == 5_000_000


def _res(html, status=200, title=""):
    return FetchResult("u", "u", status, html, title, NOW, 0.1)


def test_classify_page():
    L = listing(1, 1)
    assert classify_page(_res(item_html(L)), "item") == "ok"
    assert classify_page(_res(search_html([L], 1, False)), "search") == "ok"
    assert classify_page(_res(BLOCK_HTML, 200, "Доступ ограничен: проблема с IP"), "search") == "blocked"
    assert classify_page(_res("<html></html>", 429), "item") == "blocked"
    assert classify_page(_res("<html>404</html>", 404), "item") == "not_found"
    assert classify_page(_res("<html><p>Ничего не найдено</p></html>"), "search") == "empty"
    assert classify_page(_res("<html><p>что-то другое</p></html>"), "search") == "unexpected"


def test_price_split_points():
    mid = AvitoCrawler._split(0, 2_000_000_000)
    assert 0 < mid < 2_000_000_000 and mid % 10_000 == 0
    assert 1_000_000 < AvitoCrawler._split(5_000_000, 6_000_000) < 6_000_000
