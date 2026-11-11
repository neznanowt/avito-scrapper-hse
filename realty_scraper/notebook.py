"""Помощники для работы из Google Colab / Jupyter.

Основная схема: сбор идёт на компьютере (Windows, режим connect к Chrome), данные пишутся в папку
Google Drive, а копия базы после каждого запуска кладётся туда же (db_copy_to). Ноутбук в Colab берёт
снимок этой копии на локальный диск и показывает отчёты, таблицы и фото — ничего не записывая в базу.

DbSync/run_cli нужны для запасного варианта, когда сбор запускают прямо из Colab (сервер, прокси).
"""

from __future__ import annotations

import base64
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

import yaml

from .db import copy_db as sqlite_copy

EXIT_MEANING = {0: "успешно", 1: "ошибка — смотрите журнал выше", 2: "не уложились в лимит времени",
                3: "Авито блокирует запросы"}


def snapshot_db(shared: Path, local: Path) -> Path:
    """Свежий снимок общей базы с Drive на локальный диск — только для чтения."""
    shared, local = Path(shared), Path(local)
    if not shared.exists():
        raise FileNotFoundError(f"На Drive пока нет базы ({shared}). Она появится после первого запуска "
                                "сборщика на компьютере (windows\\run.bat → пункт 1 или 2).")
    sqlite_copy(shared, local)
    when = datetime.fromtimestamp(shared.stat().st_mtime)
    print(f"Снимок базы от {when:%d.%m.%Y %H:%M} ({shared.stat().st_size / 2**20:.1f} МБ)")
    return local


class DbSync:
    """База на Drive (общая) <-> локальная копия в сессии.

    Защита от затирания: если базу на Drive изменил кто-то другой после того, как вы её скачали,
    push не перезапишет её, а сохранит вашу версию рядом как *_conflict_*.sqlite.
    """

    def __init__(self, shared: Path, local: Path):
        self.shared = Path(shared)
        self.local = Path(local)
        self.marker = self.local.with_name(self.local.name + ".pulled")

    def _shared_mtime(self) -> float | None:
        return self.shared.stat().st_mtime if self.shared.exists() else None

    def _write_marker(self):
        m = self._shared_mtime()
        self.marker.write_text("" if m is None else repr(m))

    def pull(self, force: bool = False) -> str:
        self.local.parent.mkdir(parents=True, exist_ok=True)
        if self.local.exists() and not force:
            return f"Локальная база уже есть ({self.local}), использую её."
        if not self.shared.exists():
            self._write_marker()
            return "На Drive базы ещё нет — начинаем с пустой."
        sqlite_copy(self.shared, self.local)
        self._write_marker()
        return f"База скопирована с Drive: {self.shared} → {self.local}"

    def push(self, force: bool = False) -> str:
        if not self.local.exists():
            return "Локальной базы нет — копировать нечего."
        pulled = self.marker.read_text().strip() if self.marker.exists() else ""
        pulled_m = float(pulled) if pulled else None
        cur = self._shared_mtime()
        if not force and cur is not None and (pulled_m is None or abs(cur - pulled_m) > 1):
            conflict = self.shared.with_name(f"{self.shared.stem}_conflict_{datetime.now():%Y%m%d_%H%M%S}.sqlite")
            sqlite_copy(self.local, conflict)
            raise RuntimeError(
                "База на Drive изменилась после того, как вы её скачали (кто-то ещё запускал сбор?). "
                f"Ваша версия сохранена как {conflict.name}; базу на Drive я не трогал. "
                "Договоритесь, чья версия главная, и при необходимости вызовите sync.push(force=True)."
            )
        sqlite_copy(self.local, self.shared)
        self._write_marker()
        return f"База сохранена на Drive ({self.shared.stat().st_size / 2**20:.1f} МБ)"


def get_or_create_salt(project_dir: Path) -> str:
    """Соль для псевдонима продавца — одна на всю команду и на всё время сбора, хранится на Drive."""
    p = Path(project_dir) / "seller_salt.txt"
    if p.exists():
        return p.read_text().strip()
    salt = secrets.token_hex(16)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(salt)
    return salt


def write_config(path: Path, *, data_dir: Path, db_file: Path, salt: str, proxy: str | None = None,
                 headless: bool = True, channel: str | None = None, profile_dir: Path | None = None,
                 extra: dict | None = None) -> Path:
    """profile_dir — профиль браузера на локальном диске: Chrome не работает с профилем на Google Drive."""
    browser = {"headless": headless, "channel": channel, "proxy": proxy or None}
    if profile_dir:
        browser["user_data_dir"] = str(profile_dir)
    cfg = {
        "data_dir": str(data_dir),
        "db_file": str(db_file),
        "seller_hash_salt": salt,
        "browser": browser,
    }
    for k, v in (extra or {}).items():
        if isinstance(v, dict):
            cfg.setdefault(k, {}).update(v)
        else:
            cfg[k] = v
    path = Path(path)
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def run_cli(args: list[str], *, code_dir: Path, config: Path, sync: DbSync | None = None,
            push_every_s: float = 600, xvfb: bool = False) -> int:
    """Запускает `python -m realty_scraper ...` отдельным процессом с живым выводом.

    Отдельный процесс нужен потому, что синхронный Playwright не работает внутри цикла событий ноутбука.
    Во время работы база каждые push_every_s секунд копируется на Drive, после завершения — ещё раз.
    """
    cmd = [sys.executable, "-m", "realty_scraper", "--config", str(config), *map(str, args)]
    if xvfb:
        cmd = ["xvfb-run", "-a", "--server-args=-screen 0 1366x900x24", *cmd]
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONPATH=str(code_dir), REALTY_NOTEBOOK="1")
    proc = subprocess.Popen(cmd, cwd=code_dir, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    stop = threading.Event()

    def periodic_push():
        while not stop.wait(push_every_s):
            try:
                print(f"[{datetime.now():%H:%M}] {sync.push()}", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"Не удалось сохранить базу на Drive: {e}", flush=True)

    t = None
    if sync is not None and push_every_s:
        t = threading.Thread(target=periodic_push, daemon=True)
        t.start()
    try:
        for line in proc.stdout:
            print(line, end="", flush=True)
        code = proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        code = proc.wait()
    finally:
        stop.set()
    if sync is not None:
        print(sync.push())
    print(f"Код выхода {code}: {EXIT_MEANING.get(code, 'неизвестно')}")
    return code


# ---------- просмотр данных ----------

def listings_df(db_path: Path, platform: str = "avito"):
    import pandas as pd

    con = sqlite3.connect(db_path)
    df = pd.read_sql("SELECT * FROM listings WHERE platform=?", con, params=(platform,))
    con.close()
    return df


def fill_rates(df, columns: list[str] | None = None):
    import pandas as pd

    columns = columns or ["price", "area_total", "rooms", "floor", "area_kitchen", "ceiling_height", "bathroom",
                          "balcony", "renovation", "house_type", "build_year", "lat", "metro_name", "seller_type",
                          "description", "views_total", "photo_count"]
    parsed = df[df["last_parsed_at"].notna()]
    rows = [(c, parsed[c].notna().mean() * 100 if len(parsed) else 0) for c in columns if c in parsed]
    return pd.DataFrame(rows, columns=["поле", "заполнено, %"]).round(0).set_index("поле")


def renovation_overview(df):
    parsed = df[df["last_parsed_at"].notna()].copy()
    parsed["renovation"] = parsed["renovation"].fillna("— не указано —")
    g = parsed.groupby("renovation").agg(
        объявлений=("listing_id", "count"),
        медиана_цены_м2=("price_per_m2", "median"),
        медиана_фото=("photo_count", "median"),
        доля_агентств=("is_company", "mean"),
    )
    g["доля, %"] = (g["объявлений"] / g["объявлений"].sum() * 100).round(1)
    return g.sort_values("объявлений", ascending=False).round(2)


def photo_grid_html(db_path: Path, data_dir: Path, listing_id: str | None = None, n: int = 12,
                    platform: str = "avito", seed: int | None = None) -> str:
    """HTML-сетка уменьшенных фото одного объявления (по умолчанию — случайного с фото)."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    if listing_id is None:
        row = con.execute(
            """SELECT l.listing_id FROM listings l WHERE l.platform=? AND EXISTS
               (SELECT 1 FROM photos p WHERE p.platform=l.platform AND p.listing_id=l.listing_id
                AND p.downloaded_at IS NOT NULL) ORDER BY RANDOM() LIMIT 1""", (platform,)).fetchone()
        if not row:
            con.close()
            return "<p>Скачанных фото пока нет.</p>"
        listing_id = row["listing_id"]
    L = con.execute("SELECT * FROM listings WHERE platform=? AND listing_id=?", (platform, str(listing_id))).fetchone()
    photos = con.execute(
        """SELECT position, kind, local_path FROM photos WHERE platform=? AND listing_id=? AND is_current=1
           AND downloaded_at IS NOT NULL ORDER BY position LIMIT ?""", (platform, str(listing_id), n)).fetchall()
    con.close()
    if L is None:
        return f"<p>Объявления {listing_id} нет в базе.</p>"
    cells = []
    for p in photos:
        f = Path(data_dir) / p["local_path"]
        if not f.exists():
            continue
        b64 = base64.b64encode(f.read_bytes()).decode()
        cells.append(f'<figure style="margin:0"><img src="data:image/jpeg;base64,{b64}" '
                     f'style="width:100%;border-radius:4px"><figcaption style="font-size:11px;color:#666">'
                     f'{"планировка " if p["kind"] == "layout" else ""}#{p["position"]}</figcaption></figure>')
    price = f'{L["price"]:,}'.replace(",", " ") if L["price"] else "—"
    head = (f'<div style="font-family:sans-serif;margin-bottom:8px"><b>{L["title"] or ""}</b> · {price} ₽ · '
            f'ремонт: <b>{L["renovation"] or "не указан"}</b> · {L["house_type"] or ""} {L["build_year"] or ""} · '
            f'{L["seller_type"] or ""} · <a href="{L["url"]}" target="_blank">{listing_id}</a></div>')
    grid = ('<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:6px">'
            + "".join(cells) + "</div>")
    return head + (grid if cells else "<p>Фото этого объявления ещё не скачаны.</p>")


def latest_report(data_dir: Path) -> str | None:
    files = sorted((Path(data_dir) / "reports").glob("*.md"), key=lambda p: p.stat().st_mtime)
    return files[-1].read_text(encoding="utf-8") if files else None

