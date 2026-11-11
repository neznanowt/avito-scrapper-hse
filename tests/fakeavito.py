"""Генератор страниц, повторяющих структуру Авито (снято с живых страниц 11.11.2026),
и локальный HTTP-сервер для интеграционного теста. Данные синтетические."""

from __future__ import annotations

import html as htmlmod
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from PIL import Image, ImageDraw

SEARCH_PATH = "/sankt-peterburg/kvartiry/prodam/vtorichka-ASgBAgICAkSSA8YQ5geMUg"


def listing(lid: int, price: int, area: float = 50.0, rooms: str = "2", floor: str = "1 из 7",
            n_photos: int = 3, img_base: str = "https://c60.img.avito.st/image/1/1.", renovation="косметический",
            seller="Частное лицо", is_company=False, title=None, closed=False):
    return dict(lid=lid, price=price, area=area, rooms=rooms, floor=floor, n_photos=n_photos, img_base=img_base,
                renovation=renovation, seller=seller, is_company=is_company, closed=closed,
                title=title or f"{rooms}-к. квартира, {str(area).replace('.', ',').rstrip('0').rstrip(',')} м², {floor.replace(' из ', '/')} эт.")


def item_url_path(L) -> str:
    return f"/sankt-peterburg/kvartiry/{L['rooms']}-k._kvartira_{int(L['area'])}_m_{L['lid']}"


def hydration_data(L) -> dict:
    sizes = lambda k: {  # noqa: E731
        "1280x960": f"{L['img_base']}{L['lid']}_{k}_1280.jpg",
        "150x110": f"{L['img_base']}{L['lid']}_{k}_150.jpg",
        "640x480": f"{L['img_base']}{L['lid']}_{k}_640.jpg",
        "75x55": f"{L['img_base']}{L['lid']}_{k}_75.jpg",
    }
    params = [
        ("Количество комнат", L["rooms"]), ("Общая площадь", f"{L['area']} м²".replace(".", ",")),
        ("Площадь кухни", "8 м²"), ("Жилая площадь", "32 м²"), ("Этаж", L["floor"]),
        ("Тип комнат", "изолированные"), ("Высота потолков", "3 м"), ("Санузел", "раздельный"),
        ("Окна", "во двор"), ("Ремонт", L["renovation"]), ("Стоимость ремонта", "от 643 000 ₽ за 50 м²"),
        ("Мебель", "кухня, спальные места"), ("Способ продажи", "свободная"),
    ]
    house = [("Тип дома", "кирпичный"), ("Год постройки", "1955"), ("Этажей в доме", "7"),
             ("Пассажирский лифт", "1"), ("В доме", "газ"), ("Двор", "закрытая территория, детская площадка")]
    item = {
        "id": L["lid"], "title": L["title"], "url": item_url_path(L), "price": L["price"],
        "formattedPrice": {"value": L["price"], "string": f"{L['price']:,}".replace(",", " ")},
        "description": "Продаётся уютная сталинка. 2 изолированных комнаты, свежий косметический ремонт.\nРядом метро.",
        "address": "Санкт-Петербург, ул. Куйбышева, 1/5",
        "geo": {"address": "Санкт-Петербург, ул. Куйбышева, 1/5", "coords": {"lat": 59.954529, "lng": 30.32698, "zoom": 16},
                "references": [
                    {"after": " 500 м", "afterWithIcon": {"iconName": "walkingRoute16", "text": "6–10 мин."},
                     "colors": ["#0078C9"], "content": "Горьковская"},
                    {"after": " 1,6 км", "afterWithIcon": {"iconName": "walkingRoute16", "text": "16–20 мин."},
                     "colors": ["#0078C9"], "content": "Петроградская"}]},
        "location": {"coords": {"lat": 59.939095, "lng": 30.315868, "zoom": 16}},  # центр города — не адрес!
        "imageUrls": [sizes(k) for k in range(L["n_photos"])],
        "images": [59325024363 + k for k in range(L["n_photos"])],
        "houseParams": {"data": {"config": {}, "items": [{"attributeId": i, "title": t, "description": d}
                                                         for i, (t, d) in enumerate(house)], "title": "О доме"}},
        "flags": {"isSecondarySell": True, "isDevelopmentSell": False},
        "categoryId": 24, "microCategoryId": 4928, "statusId": 4,
        "sortFormatedDate": "сегодня в 14:04", "finishTime": 1796987047,
        "isActive": not L["closed"], "isClosed": L["closed"], "isArchived": False, "isDeleted": False,
        "isExpired": False, "isBlocked": False,
        "userId": 123456789, "shop": {"isShop": False, "id": None},
        "seller": {"isRealtyDeveloper": False},
        "realtyLayoutUrls": [],
    }
    buyer = {
        "item": item,
        "paramsDto": {"items": [{"attributeId": i, "title": t, "description": d} for i, (t, d) in enumerate(params)]},
        "paramsBlock": {"items": []},
        "seller": {"labels": {"nominative": L["seller"]}},
        "isCompany": L["is_company"], "houseId": 22500, "addressId": 653240,
        "viewStat": {"todayViews": 3, "totalViews": 120},
        "priceHistory": {"hasIncreased": False, "isNew": True, "recordsCount": 1},
        "closedItem": L["closed"], "closedItemStatus": "Снято с публикации" if L["closed"] else "",
        "galleryInfo": {"media": [{"isVideo": False, "urls": sizes(k)} for k in range(L["n_photos"])]},
    }
    return {"loaderData": {"0": None, "catalog-or-main-or-item": {"buyerItem": buyer}}, "actionData": None, "errors": None}


def item_html(L) -> str:
    payload = json.dumps(json.dumps(hydration_data(L), ensure_ascii=False), ensure_ascii=False)
    return f"""<!doctype html><html><head><title>{L['title']} | Авито ({L['lid']})</title></head><body>
<div data-marker="item-view/title-info"><h1>{L['title']}</h1></div>
<div data-marker="item-view/main-gallery"></div>
<script>(()=>{{var e=1}})()</script>
<script>window.__staticRouterHydrationData = JSON.parse({payload});</script>
<script>window.__mfe__ = "{{}}";
window.__preloadedState__ = "{{\\"abCentral\\":{{}}}}";</script>
<span data-marker="item-view/item-id">№ {L['lid']}</span>
</body></html>"""


def item_dom_only_html(L) -> str:
    """Карточка без встроенного JSON — для проверки запасного разбора."""
    return f"""<!doctype html><html><head><title>{L['title']}</title>
<link rel="canonical" href="https://www.avito.ru{item_url_path(L)}"></head><body>
<div data-marker="item-view/title-info"><h1>{L['title']}</h1></div>
<div data-marker="item-view/main-gallery"><img src="https://10.img.avito.st/image/1/1.aaa"><img src="https://10.img.avito.st/image/1/1.bbb"></div>
<div data-marker="item-view/item-params"><h2>О квартире</h2><ul>
<li>Количество комнат: {L['rooms']}</li><li>Общая площадь: {L['area']} м²</li><li>Этаж: {L['floor']}</li>
<li>Санузел: совмещенный</li><li>Ремонт: {L['renovation']}</li><li>Балкон или лоджия: балкон</li></ul></div>
<h2>О доме</h2><ul><li>Тип дома: панельный</li><li>Год постройки: 1968</li><li>Этажей в доме: 9</li></ul>
<div data-marker="item-view/item-description">Квартира в хорошем состоянии.</div>
<div data-marker="item-map-wrapper" data-map-lat="59.9" data-map-lon="30.3" data-item-id="{L['lid']}"></div>
<span itemprop="address">Санкт-Петербург, пр. Просвещения, 10</span>
<span data-marker="item-view/item-price" content="{L['price']}">{L['price']} ₽</span>
<span data-marker="item-view/item-id">№ {L['lid']}</span>
<span data-marker="item-view/item-date"> · вчера в 9:15</span>
<span data-marker="item-view/total-views">1 205 просмотров</span><span data-marker="item-view/today-views">(+7 сегодня)</span>
<span data-marker="seller-info/label">Агентство</span>
</body></html>"""


def card_html(L, region="sankt-peterburg") -> str:
    href = item_url_path(L).replace("/sankt-peterburg/", f"/{region}/") + "?context=H4sIAAAAAAAA_wE_AMD"
    price = f"{L['price']:,}".replace(",", " ")
    return f"""<div data-marker="item" data-item-id="{L['lid']}" id="i{L['lid']}" itemscope itemtype="http://schema.org/Product">
<a data-marker="item-photo-sliderLink" href="{href}"></a>
<div data-marker="item-price"><meta itemprop="priceCurrency" content="RUB"><meta itemprop="price" content="{L['price']}">
<span data-marker="item-price-value">{price}&nbsp;₽</span></div>
<p>470 000 ₽ за м²</p>
<a data-marker="item-title" itemprop="url" href="{href}"><h3 itemprop="name">{htmlmod.escape(L['title'])}</h3></a>
<div data-marker="item-address"><p>ул. Куйбышева, 1/5</p><a data-marker="metro_link">Горьковская</a></div>
<p data-marker="item-date">2 часа назад</p></div>"""


def search_html(cards: list, total: int, has_next: bool, foreign: list | None = None) -> str:
    offers = [{"@type": "Offer", "price": L["price"], "priceCurrency": "RUB",
               "url": "https://www.avito.ru" + item_url_path(L), "name": L["title"]} for L in cards]
    ld = {"@context": "https://schema.org", "@graph": [{"@type": "Product", "name": "Купить квартиру",
          "offers": {"@type": "AggregateOffer", "offerCount": total, "offers": offers}}]}
    nxt = '<a data-marker="pagination-button/nextPage" href="#">→</a>' if has_next else ""
    body = "\n".join(card_html(L) for L in cards) + "\n".join(card_html(L, "leningradskaya_oblast") for L in foreign or [])
    return f"""<!doctype html><html><head><title>Купить квартиру во вторичке в Санкт-Петербурге | Авито</title>
<script type="application/ld+json" data-rh="true">{json.dumps(ld, ensure_ascii=False)}</script></head><body>
<h1>Купить квартиру во вторичке в Санкт-Петербурге<span data-marker="page-title/count">{total:,}</span></h1>
<div data-marker="catalog-serp">{body}</div>
<nav>{nxt}</nav></body></html>""".replace(f"{total:,}", f"{total:,}".replace(",", " "))


BLOCK_HTML = """<!doctype html><html><head><title>Доступ ограничен: проблема с IP</title></head>
<body><h2>Доступ ограничен: проблема с IP</h2><p>Подтвердите, что запросы отправляли вы</p></body></html>"""


def jpeg(seed: int, size=(640, 480)) -> bytes:
    img = Image.new("RGB", size, ((seed * 37) % 255, (seed * 91) % 255, (seed * 53) % 255))
    d = ImageDraw.Draw(img)
    for k in range(6):
        x = (seed * 13 + k * 97) % size[0]
        d.rectangle([x, k * 60, x + 120, k * 60 + 90], fill=((k * 40) % 255, 200 - k * 20, (seed * 7) % 255))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


class FakeAvito:
    """Локальный сервер: выдача с фильтром pmin/pmax и пагинацией, карточки, картинки."""

    def __init__(self, listings: list, per_page: int = 3, missing_items: set | None = None, foreign: list | None = None):
        self.listings = listings
        self.per_page = per_page
        self.missing_items = missing_items or set()
        self.foreign = foreign or []
        self.requests: list[str] = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                fake.requests.append(self.path)
                u = urlsplit(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                if u.path == SEARCH_PATH:
                    lo, hi = int(q.get("pmin", 0)), int(q.get("pmax", 10**12))
                    sel = [L for L in fake.listings if lo <= L["price"] <= hi]
                    page = int(q.get("p", 1))
                    chunk = sel[(page - 1) * fake.per_page: page * fake.per_page]
                    has_next = page * fake.per_page < len(sel)
                    return self._send(200, search_html(chunk, len(sel), has_next, fake.foreign if page == 1 else None))
                if u.path.startswith("/img/"):
                    seed = sum(map(ord, u.path))
                    return self._send(200, jpeg(seed), "image/jpeg")
                for L in fake.listings:
                    if u.path == item_url_path(L):
                        if L["lid"] in fake.missing_items:
                            return self._send(404, "<html><title>Такой страницы нет</title></html>")
                        return self._send(200, item_html(L))
                return self._send(404, "<html><title>404</title></html>")

            def _send(self, code, body, ctype="text/html; charset=utf-8"):
                data = body.encode("utf-8") if isinstance(body, str) else body
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        for L in self.listings:
            L["img_base"] = f"{self.base_url}/img/"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.server.shutdown()
