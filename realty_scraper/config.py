"""Конфигурация. Значения по умолчанию подходят для пилота; переопределяются в config.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path

import yaml


@dataclass
class BrowserConfig:
    channel: str | None = "chrome"  # установленный Google Chrome; None — встроенный Chromium Playwright
    headless: bool = False  # Авито хуже относится к headless; на сервере без экрана — xvfb-run
    user_data_dir: str = "browser_profile"  # относительно data_dir; куки сохраняются между запусками
    proxy: str | None = None  # "http://user:pass@host:port"
    user_agent: str | None = None  # None — родной UA браузера (в headless убирается слово Headless)
    locale: str = "ru-RU"
    timezone: str = "Europe/Moscow"
    viewport_width: int = 1366
    viewport_height: int = 900
    page_timeout_s: int = 60
    block_resources: list[str] = field(default_factory=lambda: ["image", "media", "font"])


@dataclass
class PacingConfig:
    delay_min_s: float = 7.0  # пауза между страницами
    delay_max_s: float = 16.0
    long_pause_every: int = 40  # каждые N страниц — длинная пауза
    long_pause_min_s: float = 60.0
    long_pause_max_s: float = 180.0
    block_pause_s: float = 900.0  # пауза после блокировки
    max_block_retries: int = 2  # после стольких блокировок подряд запуск останавливается
    wait_for_operator: bool = False  # в видимом браузере ждать, пока человек пройдёт проверку
    operator_wait_max_s: float = 900.0


@dataclass
class AvitoConfig:
    base_url: str = "https://www.avito.ru"
    region_slug: str = "sankt-peterburg"  # объявления других регионов в выдаче отбрасываются
    search_path: str = "/sankt-peterburg/kvartiry/prodam/vtorichka-ASgBAgICAkSSA8YQ5geMUg"
    sort: int | None = 104  # 104 — по дате
    max_pages_per_query: int = 100  # больше Авито не отдаёт
    slice_threshold: int = 4800  # если в запросе больше объявлений — делим ценовой диапазон
    price_min: int = 0
    price_max: int = 2_000_000_000
    min_slice_width: int = 50_000
    save_raw_items: bool = True
    save_raw_search: bool = False
    refresh_after_days: int = 30  # повторный разбор карточки (просмотры, правки описания, фото)
    max_new_items_per_run: int = 1500
    max_refresh_per_run: int = 300
    max_verify_per_run: int = 300  # проверка пропавших из выдачи (сняты/проданы?)
    image_sizes: list[str] = field(default_factory=lambda: ["640x480", "1280x960"])


@dataclass
class PhotoConfig:
    max_side: int = 384
    jpeg_quality: int = 85
    delay_min_s: float = 0.2
    delay_max_s: float = 0.6
    timeout_s: float = 30.0
    max_attempts: int = 3
    max_per_run: int = 60_000


@dataclass
class Config:
    data_dir: str = "data"
    db_name: str = "realty.sqlite"
    seller_hash_salt: str = "change-me"
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    pacing: PacingConfig = field(default_factory=PacingConfig)
    avito: AvitoConfig = field(default_factory=AvitoConfig)
    photos: PhotoConfig = field(default_factory=PhotoConfig)
    base_dir: Path = field(default_factory=Path.cwd, repr=False)

    @property
    def data_path(self) -> Path:
        p = Path(self.data_dir)
        return p if p.is_absolute() else (self.base_dir / p)

    @property
    def db_path(self) -> Path:
        return self.data_path / self.db_name

    def path(self, *parts: str) -> Path:
        return self.data_path.joinpath(*parts)


def _merge(obj, data: dict, where: str):
    names = {f.name: f for f in fields(obj)}
    for key, value in (data or {}).items():
        if key not in names or key == "base_dir":
            raise ValueError(f"Неизвестный параметр конфигурации: {where}{key}")
        current = getattr(obj, key)
        if is_dataclass(current):
            if not isinstance(value, dict):
                raise ValueError(f"{where}{key} должен быть словарём")
            _merge(current, value, f"{where}{key}.")
        else:
            setattr(obj, key, value)


def load_config(path: str | os.PathLike | None = None) -> Config:
    """Читает YAML. Путь: аргумент, переменная REALTY_CONFIG или ./config.yaml (если есть)."""
    path = path or os.environ.get("REALTY_CONFIG")
    if path is None and Path("config.yaml").exists():
        path = "config.yaml"
    cfg = Config()
    if path:
        p = Path(path).resolve()
        with open(p, encoding="utf-8") as f:
            _merge(cfg, yaml.safe_load(f) or {}, "")
        cfg.base_dir = p.parent
    return cfg
