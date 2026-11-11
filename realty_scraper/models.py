"""Структуры данных, общие для всех площадок (Авито, Циан, Домклик)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


class ParseError(Exception):
    """Страница загрузилась, но данных объявления в ней нет."""


class BlockedError(Exception):
    """Площадка блокирует сбор; запуск нужно остановить."""


class DeadlineReached(Exception):
    """Исчерпан отведённый на запуск бюджет времени."""


@dataclass
class SearchCard:
    listing_id: str
    url: str
    title: str | None = None
    price: int | None = None
    date_label: str | None = None
    address_raw: str | None = None


@dataclass
class SearchPage:
    total_count: int | None
    cards: list[SearchCard]
    has_next: bool
    foreign_skipped: int = 0  # карточки из других регионов / рекомендации
    empty: bool = False


@dataclass
class PhotoRef:
    position: int
    url: str  # размер для скачивания (уменьшаем до 384 px)
    url_large: str | None = None
    kind: str = "photo"  # photo | layout


@dataclass
class ParsedItem:
    listing: dict  # ключи совпадают с колонками таблицы listings
    photos: list[PhotoRef] = field(default_factory=list)
    status: str = "active"  # active | closed
    source: str = "hydration"  # hydration | dom


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int | None
    html: str
    title: str
    fetched_at: datetime
    elapsed_s: float
