"""Отчёт о сборе: заполненность полей, фото, скорость и прогноз объёма полного сбора.

pilot_report — по одному запуску (сразу после пилота);
full_report — по всей базе и всем запускам (если пилот шёл частями, например после обрыва сессии Colab).
"""

from __future__ import annotations

import json
import statistics
from collections import Counter
from datetime import datetime

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
FETCH_MODES = ("pilot", "daily", "search", "items_new", "items_refresh", "items_verify")


def _pct(n, d):
    return f"{100 * n / d:.0f}%" if d else "—"


def _top(rows, field, k=8):
    c = Counter((r[field] or "— нет —") for r in rows)
    return ", ".join(f"{v} ({_pct(n, len(rows))})" for v, n in c.most_common(k))


def _filled(r, f):
    if f == "rooms_any":
        return r["rooms"] is not None or bool(r["is_studio"]) or bool(r["is_free_layout"])
    if f == "has_photos":
        return (r["photo_count"] or 0) > 0
    return r[f] not in (None, "")


def _avg_photo_kb(con, platform):
    row = con.execute("SELECT COUNT(*) n, AVG(bytes) b, SUM(downloaded_at IS NULL AND attempts > 0) bad "
                      "FROM photos WHERE platform=? AND (downloaded_at IS NOT NULL OR attempts > 0)",
                      (platform,)).fetchone()
    return (row["b"] / 1024 if row["b"] else None), row["n"] or 0, row["bad"] or 0


def _render(con, platform, header, rows, agg, elapsed_s, fetch_item_s, errs) -> str:
    n = len(rows)
    total = agg.get("total_in_query")
    out = [header, "",
           "## Объём", "",
           f"- Объявлений в выдаче по запросу: **{total if total is not None else '—'}**",
           f"- Страниц выдачи загружено: {agg.get('search_pages', 0)}; карточек из выдачи: {agg.get('cards', 0)}",
           (f"- Карточек разобрано: **{n}**; ошибок загрузки {agg.get('failed', 0)}, "
            f"разбора {agg.get('parse_error', 0)}, запасной разбор по HTML: {agg.get('dom_fallback', 0)}"),
           ""]
    if not n:
        out.append("Нет разобранных карточек — смотрите журнал и таблицу errors.")
        return "\n".join(out)

    out += ["## Заполненность полей", "", "| Поле | Заполнено |", "|---|---|"]
    out += [f"| {label} | {_pct(sum(_filled(r, f) for r in rows), n)} |" for f, label in KEY_FIELDS]
    out += ["", "## Распределения", "",
            f"- Ремонт: {_top(rows, 'renovation')}",
            f"- Продавец: {_top(rows, 'seller_type')}",
            f"- Тип дома: {_top(rows, 'house_type')}",
            f"- Санузел: {_top(rows, 'bathroom')}",
            (f"- Апартаменты: {sum(bool(r['is_apartments']) for r in rows)}, "
             f"доли: {sum(bool(r['is_share']) for r in rows)}, "
             f"от застройщика: {sum(bool(r['is_development']) for r in rows)}, "
             f"без фото: {sum(not (r['photo_count'] or 0) for r in rows)}"),
            ""]

    counts = sorted(r["photo_count"] or 0 for r in rows)
    mean_ph = statistics.mean(counts)
    avg_kb, ph_n, ph_bad = _avg_photo_kb(con, platform)
    out += ["## Фото", "",
            (f"- На объявление: в среднем {mean_ph:.1f}, медиана {statistics.median(counts):.0f}, "
             f"от {counts[0]} до {counts[-1]}; с ≥5 фото — {_pct(sum(c >= 5 for c in counts), n)}"),
            f"- Планировок отдельным полем: {sum(r['layout_count'] or 0 for r in rows)}"]
    if avg_kb:
        out.append(f"- Скачано {ph_n - ph_bad}, ошибок {ph_bad}; средний файл после уменьшения до 384 px: "
                   f"**{avg_kb:.0f} КБ**")
    out.append("")

    pages = agg.get("search_pages", 0) + agg.get("selected", 0)
    out += ["## Скорость и прогноз", ""]
    if fetch_item_s:
        out.append(f"- Загрузка карточки: {fetch_item_s:.1f} с (без пауз)")
    if pages and elapsed_s:
        wall = elapsed_s / pages
        out.append(f"- С учётом пауз: ~{wall:.0f} с на страницу")
        if total:
            out.append(f"- Первичный обход всех {total} карточек: ~{total * wall / 3600:.0f} ч "
                       f"(≈{total * wall / 3600 / 7:.0f} ночей по 7 ч)")
            search_pages = total / 60 * 1.1
            out.append(f"- Ежедневный снимок выдачи (~{search_pages:.0f} страниц): ~{search_pages * wall / 3600:.1f} ч")
            if avg_kb:
                out.append(f"- Фото для {total} объявлений: ~{total * mean_ph * avg_kb / 2**20:.1f} ГБ")
    out.append("")
    if errs:
        out += ["## Ошибки", "", *[f"- {e['kind']}: {e['c']}" for e in errs], ""]
    return "\n".join(out)


def _sum_stats(stats: dict, agg: Counter):
    s = stats.get("search") or {}
    for k in ("search_pages", "cards", "foreign_skipped"):
        agg[k] += s.get(k, 0) or 0
    if s.get("total_in_query"):
        agg["total_in_query"] = s["total_in_query"]
    for key, st in stats.items():
        if key.startswith("items") and isinstance(st, dict):
            for k in ("selected", "parsed", "failed", "parse_error", "dom_fallback"):
                agg[k] += st.get(k, 0) or 0


def pilot_report(con, run_id: int, platform: str, stats: dict, timings: dict, run_started) -> str:
    rows = con.execute(
        """SELECT l.* FROM listings l JOIN observations o
             ON o.platform=l.platform AND o.listing_id=l.listing_id
           WHERE o.run_id=? AND o.source='item' AND l.platform=?""",
        (run_id, platform),
    ).fetchall()
    agg = Counter()
    _sum_stats(stats, agg)
    errs = con.execute("SELECT kind, COUNT(*) c FROM errors WHERE run_id=? GROUP BY kind ORDER BY c DESC",
                       (run_id,)).fetchall()
    fetch_item = statistics.mean(timings["item"]) if timings.get("item") else None
    elapsed = stats.get("fetch_elapsed_s") or (now_msk() - run_started).total_seconds()
    header = (f"# Пилот: отчёт о сборе ({platform})\n\n"
              f"Запуск №{run_id}, {run_started:%d.%m.%Y %H:%M} – {now_msk():%H:%M} (МСК)")
    return _render(con, platform, header, rows, agg, elapsed, fetch_item, errs)


def full_report(con, platform: str) -> str:
    rows = con.execute("SELECT * FROM listings WHERE platform=? AND last_parsed_at IS NOT NULL",
                       (platform,)).fetchall()
    agg = Counter()
    elapsed = 0.0
    runs = con.execute("SELECT * FROM runs WHERE platform=? AND finished_at IS NOT NULL ORDER BY run_id",
                       (platform,)).fetchall()
    for r in runs:
        stats = json.loads(r["stats_json"] or "{}")
        _sum_stats(stats, agg)
        if r["mode"] in FETCH_MODES:  # время загрузки страниц без скачивания фото
            elapsed += stats.get("fetch_elapsed_s") or (
                datetime.fromisoformat(r["finished_at"]) - datetime.fromisoformat(r["started_at"])).total_seconds()
    errs = con.execute("SELECT kind, COUNT(*) c FROM errors WHERE platform=? GROUP BY kind ORDER BY c DESC",
                       (platform,)).fetchall()
    period = f"{runs[0]['started_at'][:16]} – {runs[-1]['finished_at'][:16]}" if runs else "запусков нет"
    header = f"# Отчёт о сборе по всей базе ({platform})\n\nЗапусков: {len(runs)}, {period} (МСК)"
    return _render(con, platform, header, rows, agg, elapsed, None, errs)
