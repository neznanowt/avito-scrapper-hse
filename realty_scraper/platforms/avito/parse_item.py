"""Разбор карточки квартиры на Авито.

Основной источник (проверено 11.11.2026) — встроенный JSON:
    window.__staticRouterHydrationData = JSON.parse("...")
    loaderData["catalog-or-main-or-item"].buyerItem
        .item            id, title, price, description, geo{address, coords, references},
                         imageUrls[{"640x480": url, "1280x960": url, ...}], houseParams, flags,
                         sortFormatedDate, finishTime, userId, shop, realtyLayoutUrls
        .paramsDto.items [{title: "Ремонт", description: "косметический", attributeId}]
        .seller.labels.nominative   "Частное лицо" / "Агентство" / ...
        .isCompany, .houseId, .addressId, .viewStat{totalViews, todayViews},
        .priceHistory{recordsCount, hasIncreased, isNew}, .closedItem, .closedItemStatus

Если JSON не найден, работает запасной разбор по data-marker в HTML (беднее: фото могут быть неполными).
"""

from __future__ import annotations

import json
import re
from collections import deque
from datetime import datetime

from bs4 import BeautifulSoup

from ...models import ParsedItem, ParseError, PhotoRef
from ...utils import (
    MSK, clean_text, iso, parse_distance_m, parse_float, parse_floor, parse_int, parse_ru_date,
    parse_walk_minutes, seller_key,
)
from .urls import absolute, id_from_url

PARSER_VERSION = "avito-item-2026.11.1"
HYDRATION_MARK = "window.__staticRouterHydrationData"

# Заголовок параметра (в нижнем регистре) -> поле. Точное совпадение.
APT_EXACT = {
    "количество комнат": "rooms_raw",
    "общая площадь": "area_total",
    "жилая площадь": "area_living",
    "площадь кухни": "area_kitchen",
    "этаж": "floor_raw",
    "высота потолков": "ceiling_height",
    "санузел": "bathroom",
    "тип комнат": "room_layout",
    "окна": "windows",
    "ремонт": "renovation",
    "способ продажи": "sale_method",
}
HOUSE_EXACT = {
    "тип дома": "house_type",
    "год постройки": "build_year",
    "этажей в доме": "floors_total_house",
    "пассажирский лифт": "passenger_lifts",
    "грузовой лифт": "freight_lifts",
    "серия дома": "house_series",
}
PREFIX = [("балкон", "balcony"), ("лоджи", "balcony"), ("серия", "house_series")]

CLOSED_PHRASES = ("снято с публикации", "объявление снято", "срок размещения истёк", "объявление не посмотреть")


# ---------- извлечение JSON ----------

def extract_hydration(html: str) -> dict | None:
    i = html.find(HYDRATION_MARK)
    if i < 0:
        return None
    j = html.find("</script>", i)
    chunk = html[i: j if j > 0 else None]
    try:
        a = chunk.find("JSON.parse(")
        if a >= 0:
            b = chunk.rfind('")')
            if b < a:
                return None
            return json.loads(json.loads(chunk[a + len("JSON.parse("): b + 1]))
        eq = chunk.find("=")
        return json.loads(chunk[eq + 1:].strip().rstrip(";"))
    except (ValueError, TypeError):
        return None


def find_key(obj, key: str, max_depth: int = 6):
    """Первое значение ключа key при обходе в ширину (имена маршрутов у Авито меняются)."""
    queue = deque([(obj, 0)])
    while queue:
        node, depth = queue.popleft()
        if isinstance(node, dict):
            if key in node:
                return node[key]
            if depth < max_depth:
                queue.extend((v, depth + 1) for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list) and depth < max_depth:
            queue.extend((v, depth + 1) for v in node if isinstance(v, (dict, list)))
    return None


def dig(obj, *path):
    for p in path:
        if isinstance(obj, dict):
            obj = obj.get(p)
        elif isinstance(obj, list) and isinstance(p, int) and -len(obj) <= p < len(obj):
            obj = obj[p]
        else:
            return None
    return obj


def iter_params(node):
    """Пары (заголовок, значение) из paramsDto / houseParams любой вложенности."""
    if isinstance(node, dict):
        t, d = node.get("title"), node.get("description")
        if isinstance(t, str) and d is not None and not isinstance(d, (dict, list)):
            yield t, str(d)
        for k in ("items", "groups", "params", "data"):
            v = node.get(k)
            if isinstance(v, (dict, list)):
                yield from iter_params(v)
    elif isinstance(node, list):
        for x in node:
            yield from iter_params(x)


# ---------- нормализация полей ----------

def _map_params(pairs, exact: dict) -> tuple[dict, dict]:
    raw, mapped = {}, {}
    for title, value in pairs:
        t = clean_text(title)
        v = clean_text(value)
        if not t or v is None:
            continue
        raw.setdefault(t, v)
        key = t.lower().rstrip(":")
        field = exact.get(key)
        if field is None:
            field = next((f for p, f in PREFIX if key.startswith(p)), None)
        if field and field not in mapped:
            mapped[field] = v
    return raw, mapped


def _rooms(raw: str | None, title: str | None) -> tuple[int | None, bool, bool]:
    src = (raw or "").lower()
    t = (title or "").lower()
    if "студ" in src or (not src and "студия" in t):
        return 0, True, False
    if "своб" in src or (not src and "своб" in t):
        return None, False, True
    n = parse_int(src) if src else None
    if n is None:
        m = re.search(r"(\d+)-к\.", t)
        n = int(m.group(1)) if m else None
    return n, False, False


def _normalize(L: dict, apt: dict, house: dict, title: str | None):
    rooms, studio, free = _rooms(apt.get("rooms_raw"), title)
    L["rooms_raw"] = apt.get("rooms_raw")
    L["rooms"], L["is_studio"], L["is_free_layout"] = rooms, studio, free
    for f in ("area_total", "area_living", "area_kitchen", "ceiling_height"):
        L[f] = parse_float(apt.get(f))
    if L["ceiling_height"] and L["ceiling_height"] > 10:  # «270 см» и подобное
        L["ceiling_height"] = L["ceiling_height"] / 100
    floor, floors_total = parse_floor(apt.get("floor_raw"))
    if floor is None and title:
        floor, floors_total = parse_floor(re.sub(r"(\d+)/(\d+)\s*эт", r"\1 из \2", title))
    L["floor"] = floor
    L["floors_total"] = floors_total or parse_int(house.get("floors_total_house"))
    for f in ("bathroom", "balcony", "room_layout", "windows", "renovation", "sale_method"):
        L[f] = apt.get(f) if f in apt else house.get(f)
    L["house_type"] = house.get("house_type")
    L["build_year"] = parse_int(house.get("build_year"))
    L["house_series"] = house.get("house_series") or apt.get("house_series")
    L["passenger_lifts"] = parse_int(house.get("passenger_lifts"))
    L["freight_lifts"] = parse_int(house.get("freight_lifts"))
    if L.get("area_total") is None and title:  # «2-к. квартира, 50,4 м², 1/9 эт.»
        m = re.search(r"([\d.,]+)\s*м²", title)
        L["area_total"] = parse_float(m.group(1)) if m else None


def _metro(L: dict, refs: list[dict]):
    L["metro_json"] = refs or None
    if refs:
        first = refs[0]
        L["metro_name"] = first.get("name")
        L["metro_walk_min"], L["metro_walk_max"] = parse_walk_minutes(first.get("time"))
        L["metro_distance_m"] = parse_distance_m(first.get("distance"))
    else:
        L["metro_name"] = L["metro_walk_min"] = L["metro_walk_max"] = L["metro_distance_m"] = None


def _flags(L: dict, params_raw: dict, title: str | None):
    blob = " ".join(f"{k} {v}" for k, v in params_raw.items()).lower() + " " + (title or "").lower()
    L["is_apartments"] = "апартамент" in blob
    L["is_share"] = bool(re.search(r"(^|\s)дол[яиюей]\b", (title or "").lower()))


def _pick_size(urls: dict, preferred: list[str]) -> tuple[str | None, str | None]:
    if not isinstance(urls, dict) or not urls:
        return None, None

    def area(k):
        m = re.match(r"(\d+)x(\d+)", k)
        return int(m.group(1)) * int(m.group(2)) if m else 0

    by_size = sorted(((k, v) for k, v in urls.items() if isinstance(v, str)), key=lambda kv: area(kv[0]))
    if not by_size:
        return None, None
    largest = by_size[-1][1]
    for size in preferred:
        if urls.get(size):
            return urls[size], largest
    # нет нужного размера — берём наименьший не меньше 384 px по длинной стороне, иначе наибольший
    for k, v in by_size:
        m = re.match(r"(\d+)x(\d+)", k)
        if m and max(int(m.group(1)), int(m.group(2))) >= 384:
            return v, largest
    return largest, largest


def _photos(bi: dict, it: dict, preferred: list[str]) -> list[PhotoRef]:
    photos: list[PhotoRef] = []
    sources = it.get("imageUrls")
    if not sources:
        media = dig(bi, "galleryInfo", "media") or []
        sources = [m.get("urls") for m in media if isinstance(m, dict) and not m.get("isVideo")]
    for entry in sources or []:
        url, large = _pick_size(entry, preferred) if isinstance(entry, dict) else (entry, entry)
        if url:
            photos.append(PhotoRef(position=len(photos), url=url, url_large=large, kind="photo"))
    for k, entry in enumerate(it.get("realtyLayoutUrls") or []):
        url, large = _pick_size(entry, preferred) if isinstance(entry, dict) else (entry, entry)
        if isinstance(url, str):
            photos.append(PhotoRef(position=100 + k, url=url, url_large=large, kind="layout"))
    return photos


def _base_listing() -> dict:
    return {"parser_version": PARSER_VERSION}


# ---------- основной разбор ----------

def parse_item_page(html: str, fetched_at: datetime, base_url: str, salt: str = "",
                    image_sizes: list[str] | None = None) -> ParsedItem:
    image_sizes = image_sizes or ["640x480", "1280x960"]
    data = extract_hydration(html)
    bi = find_key(data, "buyerItem") if data else None
    if isinstance(bi, dict) and isinstance(bi.get("item"), dict):
        return _from_hydration(bi, fetched_at, base_url, salt, image_sizes)
    if 'data-marker="item-view/' in html:
        return _from_dom(html, fetched_at, base_url)
    raise ParseError("на странице нет данных объявления")


def _from_hydration(bi: dict, fetched_at: datetime, base_url: str, salt: str, sizes: list[str]) -> ParsedItem:
    it = bi["item"]
    if it.get("id") is None:
        raise ParseError("в JSON нет id объявления")
    L = _base_listing()
    title = clean_text(it.get("title"))
    L["listing_id"] = str(it["id"])
    L["url"] = absolute(base_url, it["url"]) if it.get("url") else None
    L["title"] = title
    L["price"] = parse_int(it.get("price")) or parse_int(dig(it, "formattedPrice", "value"))
    desc = it.get("description")
    L["description"] = desc.strip() if isinstance(desc, str) else None

    params_raw, apt = _map_params(iter_params(bi.get("paramsDto") or bi.get("paramsBlock") or {}), APT_EXACT)
    house_raw, house = _map_params(iter_params(it.get("houseParams") or {}), HOUSE_EXACT)
    L["params_json"] = params_raw or None
    L["house_params_json"] = house_raw or None
    _normalize(L, apt, house, title)
    L["price_per_m2"] = round(L["price"] / L["area_total"], 2) if L.get("price") and L.get("area_total") else None

    geo = it.get("geo") or {}
    L["address"] = clean_text(geo.get("address") or it.get("address"))
    lat, lon = dig(geo, "coords", "lat"), dig(geo, "coords", "lng")
    L["lat"] = float(lat) if lat is not None else None
    L["lon"] = float(lon) if lon is not None else None
    refs = []
    for r in geo.get("references") or []:
        if isinstance(r, dict) and r.get("content"):
            refs.append({"name": clean_text(r.get("content")),
                         "time": clean_text(dig(r, "afterWithIcon", "text")),
                         "distance": clean_text(r.get("after")),
                         "line_colors": r.get("colors")})
    _metro(L, refs)
    L["house_id"] = str(bi["houseId"]) if bi.get("houseId") else None
    L["address_id"] = str(bi["addressId"]) if bi.get("addressId") else None

    L["seller_type"] = clean_text(dig(bi, "seller", "labels", "nominative") or dig(it, "seller", "labels", "nominative"))
    L["is_company"] = bool(bi.get("isCompany")) if bi.get("isCompany") is not None else None
    L["is_shop"] = bool(dig(it, "shop", "isShop")) if dig(it, "shop", "isShop") is not None else None
    L["seller_key"] = seller_key(salt, "avito", it.get("userId"))

    L["views_total"] = parse_int(dig(bi, "viewStat", "totalViews"))
    L["views_today"] = parse_int(dig(bi, "viewStat", "todayViews"))
    L["date_label"] = clean_text(it.get("sortFormatedDate"))
    L["date_label_at"] = iso(parse_ru_date(L["date_label"], fetched_at))
    ft = it.get("finishTime")
    L["expires_at"] = iso(datetime.fromtimestamp(ft, MSK)) if isinstance(ft, (int, float)) and ft > 0 else None
    L["price_history_records"] = parse_int(dig(bi, "priceHistory", "recordsCount"))
    inc = dig(bi, "priceHistory", "hasIncreased")
    L["price_increased"] = bool(inc) if inc is not None else None
    flags = it.get("flags") or {}
    L["is_secondary"] = flags.get("isSecondarySell")
    L["is_development"] = flags.get("isDevelopmentSell")
    L["category_id"] = it.get("categoryId")
    L["micro_category_id"] = it.get("microCategoryId")
    _flags(L, params_raw, title)

    photos = _photos(bi, it, sizes)
    L["photo_count"] = sum(p.kind == "photo" for p in photos)
    L["layout_count"] = sum(p.kind == "layout" for p in photos)

    closed = any(bool(x) for x in (bi.get("closedItem"), it.get("isClosed"), it.get("isArchived"),
                                   it.get("isDeleted"), it.get("isExpired"), it.get("isBlocked")))
    L["status_detail"] = clean_text(bi.get("closedItemStatus")) if closed else None
    return ParsedItem(listing=L, photos=photos, status="closed" if closed else "active", source="hydration")


def _from_dom(html: str, fetched_at: datetime, base_url: str) -> ParsedItem:
    soup = BeautifulSoup(html, "lxml")

    def m(marker):
        return soup.select_one(f'[data-marker="{marker}"]')

    L = _base_listing()
    lid = None
    node = m("item-view/item-id")
    if node:
        lid = str(parse_int(node.get_text()) or "") or None
    mapw = m("item-map-wrapper")
    if not lid and mapw is not None:
        lid = mapw.get("data-item-id")
    canon = soup.select_one('link[rel="canonical"]')
    url = absolute(base_url, canon["href"]) if canon and canon.get("href") else None
    lid = lid or (id_from_url(url) if url else None)
    if not lid:
        raise ParseError("не удалось определить id объявления")
    L["listing_id"], L["url"] = lid, url
    h1 = soup.select_one('[data-marker="item-view/title-info"] h1') or soup.find("h1")
    title = clean_text(h1.get_text(" ")) if h1 else None
    L["title"] = title
    price_node = m("item-view/item-price") or soup.select_one('[itemprop="price"]')
    L["price"] = parse_int(price_node.get("content") or price_node.get_text(" ")) if price_node else None
    d = m("item-view/item-description")
    L["description"] = d.get_text("\n").strip() if d else None

    pairs = []
    for li in soup.find_all("li"):
        txt = clean_text(li.get_text(" "))
        mm = re.match(r"^([^:]{2,40}):\s*(.+)$", txt or "")
        if mm:
            pairs.append((mm.group(1), mm.group(2)))
    known = set(APT_EXACT) | set(HOUSE_EXACT)
    pairs = [(t, v) for t, v in pairs
             if t.lower() in known or any(t.lower().startswith(p) for p, _ in PREFIX)]
    params_raw, apt = _map_params([(t, v) for t, v in pairs if t.lower() not in HOUSE_EXACT], APT_EXACT)
    house_raw, house = _map_params([(t, v) for t, v in pairs if t.lower() in HOUSE_EXACT], HOUSE_EXACT)
    L["params_json"], L["house_params_json"] = params_raw or None, house_raw or None
    _normalize(L, apt, house, title)
    L["price_per_m2"] = round(L["price"] / L["area_total"], 2) if L.get("price") and L.get("area_total") else None

    if mapw is not None:
        L["lat"] = parse_float(mapw.get("data-map-lat"))
        L["lon"] = parse_float(mapw.get("data-map-lon"))
    addr = soup.select_one('[itemprop="address"]')
    L["address"] = clean_text(addr.get_text(" ")) if addr else None
    _metro(L, [])
    tv, td = m("item-view/total-views"), m("item-view/today-views")
    L["views_total"] = parse_int(tv.get_text()) if tv else None
    L["views_today"] = parse_int(td.get_text()) if td else None
    dn = m("item-view/item-date")
    L["date_label"] = clean_text(dn.get_text(" ").lstrip(" ·")) if dn else None
    L["date_label_at"] = iso(parse_ru_date(L["date_label"], fetched_at))
    sl = m("seller-info/label")
    L["seller_type"] = clean_text(sl.get_text(" ")) if sl else None
    _flags(L, params_raw, title)

    photos: list[PhotoRef] = []
    gallery = m("item-view/main-gallery")
    if gallery is not None:
        seen = set()
        for el in gallery.select("[data-url], img[src]"):
            u = el.get("data-url") or el.get("src")
            if u and "img.avito.st" in u and u not in seen:
                seen.add(u)
                photos.append(PhotoRef(position=len(photos), url=u, url_large=u))
    L["photo_count"] = len(photos)
    L["layout_count"] = 0
    low = soup.get_text(" ").lower()
    closed = any(p in low for p in CLOSED_PHRASES)
    return ParsedItem(listing=L, photos=photos, status="closed" if closed else "active", source="dom")
