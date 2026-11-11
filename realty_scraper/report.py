"""Отчёт по пилоту: заполненность полей, фото, скорость и прогноз объёма полного сбора."""

from __future__ import annotations

import statistics
from collections import Counter

from .utils import now_msk

KEY_FIELDS = [
    ("price", "Цена"), ("area_total", "Общая площадь"), ("rooms_any", "Комнаты (или студия)"),
    ("floor", "Этаж"), ("floors_total", "Этажность"), ("area_kitchen", "Площадь кухни"),
    ("area_living", "Жилая площадь"), ("ceiling_height", "Высота потолков"), ("bathroom", "Санузел"),
    ("balcony", "Балкон/лоджия"), ("renovation", "Поле «ремонт»"), ("house_type", "Тип дома"),
    ("build_year", "Год постройки"), ("house_series", "Серия дома"), ("lat", "Координаты"),
    ("address", "Адрес"), ("metro_name", "Метро"), ("house_id", "ID дома Авито"),
    ("seller_type", "Тип продавца"), ("description", "Описание"), ("views_total", "Просмотры"),
    ("has_photos", "Есть фото"),
]


def _pct(n, d):
    return f"{100 * n / d:.0f}%" if d else "—"


def _top(rows, field, k=8):
    c = Counter((r[field] or "— нет —") for r in rows)
    return ", ".join(f"{v} ({_pct(n, len(rows))})" for v, n in c.most_common(k))


def pilot_report(con, run_id: int, platform: str, stats: dict, timings: dict, run_started) -> str:
    rows = con.execute(
        """SELECT l.* FROM listings l JOIN observations o
             ON o.platform=l.platform AND o.listing_id=l.listing_id
           WHERE o.run_id=? AND o.source='item' AND l.platform=?""",
        (run_id, platform),
    ).fetchall()
    n = len(rows)
    out = [f"# Пилот: отчёт о сборе ({platform})", "",
           f"Запуск №{run_id}, {run_started:%d.%m.%Y %H:%M} – {now_msk():%H:%M} (МСК)", ""]
    s = stats.get("search", {})
    it = stats.get("items", {})
    ph = stats.get("photos", {})
    total = s.get("total_in_query")
    out += ["## Объём", "",
            f"- Объявлений в выдаче по запросу: **{total if total is not None else '—'}**",
            f"- Карточек из выдачи собрано: {s.get('unique_cards', 0)} (страниц выдачи: {s.get('search_pages', 0)})",
            f"- Карточек разобрано: **{n}**; ошибок загрузки {it.get('failed', 0)}, разбора {it.get('parse_error', 0)}, "
            f"запасной разбор по HTML: {it.get('dom_fallback', 0)}",
            ""]
    if not n:
        out.append("Нет разобранных карточек — смотрите журнал и таблицу errors.")
        return "\n".join(out)

    def filled(r, f):
        if f == "rooms_any":
            return r["rooms"] is not None or bool(r["is_studio"]) or bool(r["is_free_layout"])
        if f == "has_photos":
            return (r["photo_count"] or 0) > 0
        return r[f] not in (None, "")

    out += ["## Заполненность полей", "", "| Поле | Заполнено |", "|---|---|"]
    out += [f"| {label} | {_pct(sum(filled(r, f) for r in rows), n)} |" for f, label in KEY_FIELDS]
    out += ["", "## Распределения", "",
            f"- Ремонт: {_top(rows, 'renovation')}",
            f"- Продавец: {_top(rows, 'seller_type')}",
            f"- Тип дома: {_top(rows, 'house_type')}",
            f"- Санузел: {_top(rows, 'bathroom')}",
            f"- Апартаменты: {sum(bool(r['is_apartments']) for r in rows)}, доли: {sum(bool(r['is_share']) for r in rows)}, "
            f"от застройщика: {sum(bool(r['is_development']) for r in rows)}, без фото: {sum(not (r['photo_count'] or 0) for r in rows)}",
            ""]

    counts = sorted(r["photo_count"] or 0 for r in rows)
    mean_ph = statistics.mean(counts)
    out += ["## Фото", "",
            f"- На объявление: в среднем {mean_ph:.1f}, медиана {statistics.median(counts):.0f}, "
            f"от {counts[0]} до {counts[-1]}; с ≥5 фото — {_pct(sum(c >= 5 for c in counts), n)}",
            f"- Планировок отдельным полем: {sum(r['layout_count'] or 0 for r in rows)}"]
    avg_kb = None
    if ph.get("ok"):
        avg_kb = ph["bytes"] / ph["ok"] / 1024
        out.append(f"- Скачано {ph['ok']}, ошибок {ph.get('failed', 0)}; средний файл после уменьшения до 384 px: **{avg_kb:.0f} КБ**")
    out.append("")

    elapsed = (now_msk() - run_started).total_seconds()
    pages = s.get("search_pages", 0) + it.get("selected", 0)
    fetch_item = statistics.mean(timings["item"]) if timings.get("item") else None
    out += ["## Скорость и прогноз", ""]
    if fetch_item:
        out.append(f"- Загрузка карточки: {fetch_item:.1f} с (без пауз)")
    if pages:
        wall = elapsed / pages
        out.append(f"- С учётом пауз: ~{wall:.0f} с на страницу")
        if total:
            out.append(f"- Первичный обход всех {total} карточек: ~{total * wall / 3600:.0f} ч "
                       f"(≈{total * wall / 3600 / 7:.0f} ночей по 7 ч)")
            search_pages = total / 60 * 1.1
            out.append(f"- Ежедневный снимок выдачи (~{search_pages:.0f} страниц): ~{search_pages * wall / 3600:.1f} ч")
            if avg_kb:
                gb = total * mean_ph * avg_kb / 1024 / 1024
                out.append(f"- Фото для {total} объявлений: ~{gb:.1f} ГБ")
    out.append("")
    errs = con.execute("SELECT kind, COUNT(*) c FROM errors WHERE run_id=? GROUP BY kind ORDER BY c DESC",
                       (run_id,)).fetchall()
    if errs:
        out += ["## Ошибки", "", *[f"- {e['kind']}: {e['c']}" for e in errs], ""]
    return "\n".join(out)
