"""SQLite: схема и операции записи. Колонки добавляются автоматически при обновлении кода."""

from __future__ import annotations

import json
import socket
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .models import ParsedItem, SearchCard
from .utils import iso

# Порядок колонок = порядок в кодбуке (CODEBOOK.md).
LISTING_COLUMNS: list[tuple[str, str]] = [
    ("platform", "TEXT NOT NULL"),
    ("listing_id", "TEXT NOT NULL"),
    ("url", "TEXT"),
    ("first_seen_at", "TEXT"),
    ("last_seen_at", "TEXT"),
    ("missing_since", "TEXT"),
    ("status", "TEXT"),
    ("status_detail", "TEXT"),
    ("status_checked_at", "TEXT"),
    ("last_parsed_at", "TEXT"),
    ("parse_source", "TEXT"),
    ("parser_version", "TEXT"),
    ("raw_path", "TEXT"),
    ("title", "TEXT"),
    ("price", "INTEGER"),
    ("price_per_m2", "REAL"),
    ("rooms", "INTEGER"),
    ("rooms_raw", "TEXT"),
    ("is_studio", "INTEGER"),
    ("is_free_layout", "INTEGER"),
    ("area_total", "REAL"),
    ("area_living", "REAL"),
    ("area_kitchen", "REAL"),
    ("floor", "INTEGER"),
    ("floors_total", "INTEGER"),
    ("ceiling_height", "REAL"),
    ("bathroom", "TEXT"),
    ("balcony", "TEXT"),
    ("room_layout", "TEXT"),
    ("windows", "TEXT"),
    ("renovation", "TEXT"),
    ("sale_method", "TEXT"),
    ("house_type", "TEXT"),
    ("build_year", "INTEGER"),
    ("house_series", "TEXT"),
    ("passenger_lifts", "INTEGER"),
    ("freight_lifts", "INTEGER"),
    ("address", "TEXT"),
    ("lat", "REAL"),
    ("lon", "REAL"),
    ("house_id", "TEXT"),
    ("address_id", "TEXT"),
    ("metro_name", "TEXT"),
    ("metro_walk_min", "INTEGER"),
    ("metro_walk_max", "INTEGER"),
    ("metro_distance_m", "INTEGER"),
    ("metro_json", "TEXT"),
    ("seller_type", "TEXT"),
    ("is_company", "INTEGER"),
    ("is_shop", "INTEGER"),
    ("seller_key", "TEXT"),
    ("description", "TEXT"),
    ("photo_count", "INTEGER"),
    ("layout_count", "INTEGER"),
    ("views_total", "INTEGER"),
    ("views_today", "INTEGER"),
    ("date_label", "TEXT"),
    ("date_label_at", "TEXT"),
    ("expires_at", "TEXT"),
    ("price_history_records", "INTEGER"),
    ("price_increased", "INTEGER"),
    ("is_secondary", "INTEGER"),
    ("is_development", "INTEGER"),
    ("is_apartments", "INTEGER"),
    ("is_share", "INTEGER"),
    ("category_id", "INTEGER"),
    ("micro_category_id", "INTEGER"),
    ("params_json", "TEXT"),
    ("house_params_json", "TEXT"),
]
LISTING_COLUMN_NAMES = [c for c, _ in LISTING_COLUMNS]
# Поля, которые приходят из разбора карточки (остальные ведёт сам сборщик).
_ITEM_FIELDS = [
    c for c in LISTING_COLUMN_NAMES
    if c not in {"platform", "listing_id", "first_seen_at", "last_seen_at", "missing_since", "status_checked_at"}
]

PHOTO_COLUMNS: list[tuple[str, str]] = [
    ("platform", "TEXT NOT NULL"),
    ("listing_id", "TEXT NOT NULL"),
    ("url", "TEXT NOT NULL"),
    ("url_large", "TEXT"),
    ("position", "INTEGER"),
    ("kind", "TEXT"),
    ("is_current", "INTEGER DEFAULT 1"),
    ("first_seen_at", "TEXT"),
    ("last_seen_at", "TEXT"),
    ("local_path", "TEXT"),
    ("width", "INTEGER"),
    ("height", "INTEGER"),
    ("orig_width", "INTEGER"),
    ("orig_height", "INTEGER"),
    ("bytes", "INTEGER"),
    ("sha1", "TEXT"),
    ("phash", "TEXT"),
    ("downloaded_at", "TEXT"),
    ("attempts", "INTEGER DEFAULT 0"),
    ("error", "TEXT"),
]

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS listings (
    {", ".join(f"{c} {t}" for c, t in LISTING_COLUMNS)},
    PRIMARY KEY (platform, listing_id)
);
CREATE TABLE IF NOT EXISTS observations (
    platform TEXT NOT NULL,
    listing_id TEXT NOT NULL,
    obs_date TEXT NOT NULL,
    source TEXT NOT NULL,
    observed_at TEXT,
    run_id INTEGER,
    price INTEGER,
    views_total INTEGER,
    views_today INTEGER,
    status TEXT,
    search_position INTEGER,
    PRIMARY KEY (platform, listing_id, obs_date, source)
);
CREATE TABLE IF NOT EXISTS photos (
    {", ".join(f"{c} {t}" for c, t in PHOTO_COLUMNS)},
    PRIMARY KEY (platform, listing_id, url)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT,
    platform TEXT,
    started_at TEXT,
    finished_at TEXT,
    status TEXT,
    search_complete INTEGER,
    host TEXT,
    stats_json TEXT
);
CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    ts TEXT,
    platform TEXT,
    stage TEXT,
    url TEXT,
    kind TEXT,
    message TEXT
);
CREATE TABLE IF NOT EXISTS search_slices (
    run_id INTEGER,
    platform TEXT,
    price_min INTEGER,
    price_max INTEGER,
    total_count INTEGER,
    pages_crawled INTEGER,
    cards_seen INTEGER,
    complete INTEGER
);
CREATE INDEX IF NOT EXISTS ix_listings_status ON listings(platform, status, last_parsed_at);
CREATE INDEX IF NOT EXISTS ix_listings_house ON listings(house_id);
CREATE INDEX IF NOT EXISTS ix_photos_pending ON photos(platform, downloaded_at, is_current);
CREATE INDEX IF NOT EXISTS ix_photos_phash ON photos(phash);
CREATE INDEX IF NOT EXISTS ix_photos_sha1 ON photos(sha1);
CREATE INDEX IF NOT EXISTS ix_obs_run ON observations(run_id);
"""


def connect(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    _ensure_columns(con, "listings", LISTING_COLUMNS)
    _ensure_columns(con, "photos", PHOTO_COLUMNS)
    con.commit()
    return con


def copy_db(src: Path, dst: Path):
    """Согласованная копия базы (SQLite backup API работает, даже пока в базу пишет другой процесс).
    Пишем во временный файл и подменяем — читатель на той стороне не увидит половину файла."""
    import os

    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    s = sqlite3.connect(src, timeout=60)
    d = sqlite3.connect(tmp)
    with d:
        s.backup(d)
    s.close()
    d.close()
    os.replace(tmp, dst)


def export_tables(db_path: Path, out_dir: Path) -> list[tuple[Path, int]]:
    """listings.csv и photos.csv (только текущие фото). UTF-8 с BOM — Excel открывает без «кракозябр»."""
    import csv

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    result = []
    for table, name in (("listings", "listings.csv"), ("photos", "photos.csv")):
        cur = con.execute(f"SELECT * FROM {table}" + (" WHERE is_current=1" if table == "photos" else ""))
        n = 0
        with open(out_dir / name, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow([d[0] for d in cur.description])
            for row in cur:
                w.writerow(list(row))
                n += 1
        result.append((out_dir / name, n))
    con.close()
    return result


def _ensure_columns(con, table, columns):
    have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
    for name, typ in columns:
        if name not in have:
            typ = typ.replace("NOT NULL", "")
            con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")


# ---------- запуски и ошибки ----------

def start_run(con, mode: str, platform: str, started: datetime) -> int:
    cur = con.execute(
        "INSERT INTO runs(mode, platform, started_at, status, host) VALUES (?,?,?,?,?)",
        (mode, platform, iso(started), "running", socket.gethostname()),
    )
    con.commit()
    return cur.lastrowid


def finish_run(con, run_id: int, status: str, finished: datetime, stats: dict, search_complete: bool | None):
    con.execute(
        "UPDATE runs SET status=?, finished_at=?, stats_json=?, search_complete=? WHERE run_id=?",
        (status, iso(finished), json.dumps(stats, ensure_ascii=False, default=str),
         None if search_complete is None else int(search_complete), run_id),
    )
    con.commit()


def log_error(con, run_id, platform, stage, url, kind, message, ts: datetime):
    con.execute(
        "INSERT INTO errors(run_id, ts, platform, stage, url, kind, message) VALUES (?,?,?,?,?,?,?)",
        (run_id, iso(ts), platform, stage, url, kind, str(message)[:2000]),
    )
    con.commit()


def log_slice(con, run_id, platform, pmin, pmax, total, pages, cards, complete):
    con.execute(
        "INSERT INTO search_slices VALUES (?,?,?,?,?,?,?,?)",
        (run_id, platform, pmin, pmax, total, pages, cards, int(complete)),
    )
    con.commit()


# ---------- выдача ----------

def upsert_search_card(con, platform: str, card: SearchCard, run_id: int, seen: datetime, position: int):
    ts = iso(seen)
    con.execute(
        """
        INSERT INTO listings(platform, listing_id, url, title, price, first_seen_at, last_seen_at, status)
        VALUES (?,?,?,?,?,?,?, 'active')
        ON CONFLICT(platform, listing_id) DO UPDATE SET
            url = COALESCE(excluded.url, url),
            title = COALESCE(excluded.title, title),
            price = COALESCE(excluded.price, price),
            price_per_m2 = CASE WHEN excluded.price IS NOT NULL AND area_total > 0
                                THEN excluded.price * 1.0 / area_total ELSE price_per_m2 END,
            last_seen_at = excluded.last_seen_at,
            missing_since = NULL,
            status = CASE WHEN status IN ('closed', 'removed') THEN 'active' ELSE status END
        """,
        (platform, card.listing_id, card.url, card.title, card.price, ts, ts),
    )
    con.execute(
        """INSERT OR REPLACE INTO observations
           (platform, listing_id, obs_date, source, observed_at, run_id, price, status, search_position)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (platform, card.listing_id, seen.date().isoformat(), "search", ts, run_id, card.price, "active", position),
    )


def mark_missing(con, platform: str, search_started: datetime, now: datetime) -> int:
    """После ПОЛНОГО обхода выдачи: активные объявления, которых в ней не было, помечаются пропавшими."""
    cur = con.execute(
        """UPDATE listings SET missing_since = COALESCE(missing_since, ?)
           WHERE platform=? AND status='active' AND (last_seen_at IS NULL OR last_seen_at < ?)""",
        (iso(now), platform, iso(search_started)),
    )
    con.commit()
    return cur.rowcount


# ---------- карточки ----------

def select_items(con, platform: str, mode: str, limit: int | None, now: datetime,
                 refresh_after_days: int = 30, run_id: int | None = None) -> list[sqlite3.Row]:
    lim = f" LIMIT {int(limit)}" if limit else ""
    if mode == "new":
        sql = """SELECT listing_id, url FROM listings
                 WHERE platform=? AND last_parsed_at IS NULL AND status='active'
                 ORDER BY first_seen_at DESC""" + lim
        return con.execute(sql, (platform,)).fetchall()
    if mode == "run":  # объявления, увиденные в выдаче в этом запуске и ещё не разобранные
        sql = """SELECT l.listing_id, l.url FROM listings l
                 JOIN observations o ON o.platform=l.platform AND o.listing_id=l.listing_id
                 WHERE l.platform=? AND o.run_id=? AND o.source='search' AND l.last_parsed_at IS NULL
                 ORDER BY o.search_position""" + lim
        return con.execute(sql, (platform, run_id)).fetchall()
    if mode == "refresh":
        before = iso(now - timedelta(days=refresh_after_days))
        sql = """SELECT listing_id, url FROM listings
                 WHERE platform=? AND status='active' AND missing_since IS NULL
                   AND last_parsed_at IS NOT NULL AND last_parsed_at < ?
                 ORDER BY last_parsed_at""" + lim
        return con.execute(sql, (platform, before)).fetchall()
    if mode == "verify":  # пропали из выдачи — проверяем, сняты ли
        recheck = iso(now - timedelta(days=7))
        sql = """SELECT listing_id, url FROM listings
                 WHERE platform=? AND status='active' AND missing_since IS NOT NULL
                   AND (status_checked_at IS NULL OR status_checked_at < ?)
                 ORDER BY missing_since""" + lim
        return con.execute(sql, (platform, recheck)).fetchall()
    raise ValueError(f"Неизвестный режим: {mode}")


def upsert_item(con, platform: str, item: ParsedItem, run_id: int | None, parsed_at: datetime,
                raw_path: str | None, record_observation: bool = True):
    L = dict(item.listing)
    lid = str(L["listing_id"])
    ts = iso(parsed_at)
    L["status"] = item.status
    L["parse_source"] = item.source
    L["last_parsed_at"] = ts
    if raw_path:
        L["raw_path"] = raw_path
    for k in ("params_json", "house_params_json", "metro_json"):
        if isinstance(L.get(k), (dict, list)):
            L[k] = json.dumps(L[k], ensure_ascii=False)
    for k, v in list(L.items()):
        if isinstance(v, bool):
            L[k] = int(v)
    cols = [c for c in _ITEM_FIELDS if c in L]
    con.execute(
        "INSERT INTO listings(platform, listing_id, first_seen_at) VALUES (?,?,?) "
        "ON CONFLICT(platform, listing_id) DO NOTHING",
        (platform, lid, ts),
    )
    con.execute(
        f"UPDATE listings SET {', '.join(f'{c}=?' for c in cols)}, status_checked_at=? "
        "WHERE platform=? AND listing_id=?",
        [L[c] for c in cols] + [ts, platform, lid],
    )
    # фото: новые добавляем, исчезнувшие помечаем is_current=0
    urls = []
    for ph in item.photos:
        urls.append(ph.url)
        con.execute(
            """INSERT INTO photos(platform, listing_id, url, url_large, position, kind, is_current, first_seen_at, last_seen_at)
               VALUES (?,?,?,?,?,?,1,?,?)
               ON CONFLICT(platform, listing_id, url) DO UPDATE SET
                   position=excluded.position, kind=excluded.kind, url_large=excluded.url_large,
                   is_current=1, last_seen_at=excluded.last_seen_at""",
            (platform, lid, ph.url, ph.url_large, ph.position, ph.kind, ts, ts),
        )
    if urls:
        con.execute(
            f"UPDATE photos SET is_current=0 WHERE platform=? AND listing_id=? AND url NOT IN ({','.join('?' * len(urls))})",
            [platform, lid, *urls],
        )
    if record_observation:
        con.execute(
            """INSERT OR REPLACE INTO observations
               (platform, listing_id, obs_date, source, observed_at, run_id, price, views_total, views_today, status)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (platform, lid, parsed_at.date().isoformat(), "item", ts, run_id,
             L.get("price"), L.get("views_total"), L.get("views_today"), item.status),
        )
    con.commit()


def set_status(con, platform: str, listing_id: str, status: str, now: datetime, detail: str | None = None):
    con.execute(
        "UPDATE listings SET status=?, status_detail=COALESCE(?, status_detail), status_checked_at=? "
        "WHERE platform=? AND listing_id=?",
        (status, detail, iso(now), platform, listing_id),
    )
    con.commit()


# ---------- фото ----------

def photos_pending(con, platform: str, limit: int | None, max_attempts: int):
    lim = f" LIMIT {int(limit)}" if limit else ""
    return con.execute(
        """SELECT rowid AS rid, listing_id, url, url_large, position, kind, attempts FROM photos
           WHERE platform=? AND downloaded_at IS NULL AND is_current=1 AND attempts < ?
           ORDER BY listing_id, position""" + lim,
        (platform, max_attempts),
    ).fetchall()


def photo_ok(con, rid: int, info: dict, ts: datetime):
    con.execute(
        """UPDATE photos SET local_path=?, width=?, height=?, orig_width=?, orig_height=?, bytes=?,
               sha1=?, phash=?, downloaded_at=?, attempts=attempts+1, error=NULL WHERE rowid=?""",
        (info["local_path"], info["width"], info["height"], info["orig_width"], info["orig_height"],
         info["bytes"], info["sha1"], info["phash"], iso(ts), rid),
    )


def photo_fail(con, rid: int, message: str):
    con.execute("UPDATE photos SET attempts=attempts+1, error=? WHERE rowid=?", (message[:500], rid))
