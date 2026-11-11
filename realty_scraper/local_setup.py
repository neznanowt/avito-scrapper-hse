"""Настройка и обслуживание сборщика на компьютере участника (основной вариант — Windows + Chrome).

python -m realty_scraper setup   — мастер настройки: находит папку Google Drive, пишет config.yaml
python -m realty_scraper chrome  — открыть окно Chrome сборщика (например, чтобы войти на Авито)
python -m realty_scraper update  — обновить код с GitHub (git pull или скачать архив)
"""

from __future__ import annotations

import io
import os
import shutil
import sqlite3
import string
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

import yaml

from .config import BrowserConfig
from .notebook import get_or_create_salt

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROJECT_FOLDER = "Scraper_avito"
REPO_ZIP = "https://github.com/neznanowt/avito-scrapper-hse/archive/refs/heads/main.zip"
# при обновлении из архива эти пути не трогаем: окружение, настройки, данные
KEEP = {".venv", "config.yaml", "data", ".git"}


def find_drive_roots() -> list[Path]:
    """Корни «Мой диск» Google Drive для компьютера."""
    roots: list[Path] = []
    names = ("Мой диск", "My Drive")
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            for name in names:
                p = Path(f"{letter}:\\") / name
                try:
                    if p.is_dir():
                        roots.append(p)
                except OSError:
                    pass
    else:
        home = Path.home()
        cands = [home / "Google Drive" / n for n in names]
        cands += [p / n for p in (home / "Library" / "CloudStorage").glob("GoogleDrive-*") for n in names]
        cands += [p / n for p in Path("/Volumes").glob("GoogleDrive*") for n in names]
        roots = [p for p in cands if p.is_dir()]
    return roots


def local_state_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "AvitoScraper" if base else Path.home() / ".avito-scraper"


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip().strip('"')
    return answer or default


def run_setup(config_path: Path | None = None, answers: list[str] | None = None) -> Path:
    """Мастер настройки. answers — для тестов: ответы вместо ввода с клавиатуры."""
    feed = iter(answers) if answers is not None else None

    def ask(prompt, default=""):
        if feed is not None:
            return next(feed, "") or default
        return _ask(prompt, default)

    config_path = Path(config_path or PROJECT_ROOT / "config.yaml")
    print("\n=== Настройка сборщика на этом компьютере ===\n")
    if config_path.exists() and ask(f"{config_path.name} уже есть. Перезаписать? (y/n)", "n").lower() not in ("y", "д", "да", "yes"):
        print("Оставляю текущие настройки.")
        return config_path

    roots = find_drive_roots()
    if roots:
        print(f"Нашёл Google Drive: {roots[0]}")
        default_project = str(roots[0] / DEFAULT_PROJECT_FOLDER)
    else:
        print("Google Drive для компьютера не найден. Данные можно хранить локально, а для команды "
              "лучше установить «Google Диск для компьютера» и запустить настройку ещё раз.")
        default_project = str(PROJECT_ROOT / "data_project")
    project = Path(ask("Папка проекта (данные будут в её подпапке data)", default_project))
    project.mkdir(parents=True, exist_ok=True)
    data_dir = project / "data"
    state = local_state_dir()
    local_db = state / "realty.sqlite"
    shared_db = data_dir / "realty.sqlite"
    on_drive = any(str(project).startswith(str(r)) for r in roots)

    cfg = {
        "data_dir": str(data_dir),
        "db_file": str(local_db),  # база — на локальном диске, на Drive кладётся копия
        "db_copy_to": str(shared_db) if on_drive else None,
        "seller_hash_salt": get_or_create_salt(project),  # общая соль команды лежит в папке проекта
        "browser": {"mode": "connect", "cdp_url": "http://127.0.0.1:9222"},
        "pacing": {"wait_for_operator": True},
    }
    config_path.write_text("# Создано командой `python -m realty_scraper setup`.\n"
                           + yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")

    # если на Drive уже есть база (например, от пробного запуска в Colab) — берём её за основу
    if shared_db.exists() and not local_db.exists():
        from .db import copy_db
        copy_db(shared_db, local_db)
        n = sqlite3.connect(local_db).execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        print(f"Взял за основу базу с Drive: {n} объявлений.")

    print(f"\nГотово. Настройки: {config_path}")
    print(f"  данные (фото, отчёты, журналы): {data_dir}")
    print(f"  база: {local_db}" + (f"\n  копия базы для команды: {shared_db}" if on_drive else ""))
    print("Дальше: windows\\run.bat → пункт 1 (проверка).\n")
    return config_path


def open_chrome(bcfg: BrowserConfig, url: str = "https://www.avito.ru/sankt-peterburg") -> None:
    from .browser import chrome_is_listening, start_chrome

    if chrome_is_listening(bcfg.cdp_url):
        print("Окно Chrome сборщика уже открыто (порт отладки отвечает).")
        return
    start_chrome(bcfg, url)
    print("Открыл окно Chrome сборщика. Это отдельный профиль: ваши вкладки, пароли и почта ему не видны.\n"
          "Можно войти на Авито, если нужно. Окно можно свернуть, но не закрывать во время сбора.")


def run_update(root: Path = PROJECT_ROOT, zip_url: str = REPO_ZIP) -> str:
    """Обновляет код: git pull, если проект — клон git; иначе скачивает архив с GitHub и заменяет файлы кода."""
    if (root / ".git").exists() and shutil.which("git"):
        out = subprocess.run(["git", "-C", str(root), "pull", "--ff-only"], capture_output=True, text=True)
        msg = (out.stdout + out.stderr).strip()
        if out.returncode != 0:
            raise RuntimeError(f"git pull не прошёл:\n{msg}")
        return msg or "Код обновлён."
    with urllib.request.urlopen(zip_url, timeout=60) as r:
        data = r.read()
    tmp = Path(tempfile.mkdtemp())
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extractall(tmp)
    src = next(tmp.rglob("realty_scraper/__init__.py")).parent.parent
    updated = 0
    for item in src.iterdir():
        if item.name in KEEP:
            continue
        dst = root / item.name
        if dst.is_dir():
            shutil.rmtree(dst)
        elif dst.exists():
            dst.unlink()
        shutil.move(str(item), str(dst))
        updated += 1
    shutil.rmtree(tmp, ignore_errors=True)
    return f"Код обновлён из GitHub ({updated} файлов и папок). Если менялся requirements.txt — запустите windows\\setup.bat."
