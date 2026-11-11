"""Общие утилиты: разбор русских чисел и дат, время по Москве, блокировка запуска."""

from __future__ import annotations

import hashlib
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")

_SPACES = re.compile(r"[\s    ]+")
_NUM = re.compile(r"\d[\d     ]*(?:[.,]\d+)?")
_DIGIT_SPACES = re.compile(r"[     ]")

RU_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}


def now_msk() -> datetime:
    return datetime.now(MSK)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def clean_text(s) -> str | None:
    """Схлопывает пробелы (включая неразрывные и узкие) и обрезает края."""
    if s is None:
        return None
    s = _SPACES.sub(" ", str(s)).strip()
    return s or None


def parse_float(s) -> float | None:
    """'50,4 м²' -> 50.4; '23 500 000 ₽' -> 23500000.0. Берёт первое число в строке."""
    if s is None:
        return None
    if isinstance(s, bool):
        return None
    if isinstance(s, (int, float)):
        return float(s)
    m = _NUM.search(str(s))
    if not m:
        return None
    token = _DIGIT_SPACES.sub("", m.group(0).strip()).replace(",", ".")
    try:
        return float(token)
    except ValueError:
        return None


def parse_int(s) -> int | None:
    v = parse_float(s)
    return int(round(v)) if v is not None else None


def parse_floor(s) -> tuple[int | None, int | None]:
    """'1 из 7' -> (1, 7)."""
    if not s:
        return None, None
    m = re.search(r"(-?\d+)\s*из\s*(\d+)", str(s))
    if m:
        return int(m.group(1)), int(m.group(2))
    return parse_int(s), None


def parse_walk_minutes(s) -> tuple[int | None, int | None]:
    """'6–10 мин.' -> (6, 10); 'до 5 мин.' -> (0, 5); 'от 31 мин.' -> (31, None)."""
    if not s:
        return None, None
    low = str(s).lower()
    nums = [int(x) for x in re.findall(r"\d+", low)]
    if not nums:
        return None, None
    if len(nums) >= 2:
        return nums[0], nums[1]
    if "до" in low:
        return 0, nums[0]
    if "от" in low or "больше" in low or "более" in low:
        return nums[0], None
    return nums[0], nums[0]


def parse_distance_m(s) -> int | None:
    """' 500 м' -> 500; ' 1,6 км' -> 1600."""
    v = parse_float(s)
    if v is None:
        return None
    if "км" in str(s):
        v *= 1000
    return int(round(v))


def parse_ru_date(label: str | None, now: datetime) -> datetime | None:
    """Разбирает подписи Авито: 'сегодня в 14:04', 'вчера в 9:15', '3 ноября в 12:00', '3 ноября 2025'."""
    s = clean_text(label)
    if not s:
        return None
    s = s.lower().lstrip("·").strip()
    hh, mm = 0, 0
    t = re.search(r"(\d{1,2}):(\d{2})", s)
    if t:
        hh, mm = int(t.group(1)), int(t.group(2))
    now = now.astimezone(MSK)
    if "сегодня" in s:
        d = now.date()
    elif "вчера" in s:
        d = now.date() - timedelta(days=1)
    else:
        m = re.search(r"(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?", s)
        if not m or m.group(2) not in RU_MONTHS:
            return None
        year = int(m.group(3)) if m.group(3) else now.year
        try:
            d = date(year, RU_MONTHS[m.group(2)], int(m.group(1)))
        except ValueError:
            return None
        if not m.group(3) and d > now.date():
            d = date(year - 1, d.month, d.day)
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=MSK)


def seller_key(salt: str, platform: str, user_id) -> str | None:
    """Псевдоним продавца: позволяет группировать объявления одного продавца, не храня его ID."""
    if user_id in (None, "", 0):
        return None
    return hashlib.sha256(f"{salt}:{platform}:{user_id}".encode()).hexdigest()[:16]


def short_hash(s: str, n: int = 8) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:n]


class RunLock:
    """Не даёт запустить два сбора одновременно (например, ручной и по расписанию)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.fd = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pid = self._read_pid()
            if pid and _pid_alive(pid):
                raise RuntimeError(f"Уже идёт другой запуск (pid {pid}). Файл блокировки: {self.path}") from None
            self.path.unlink(missing_ok=True)  # осталась от упавшего процесса
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(self.fd, str(os.getpid()).encode())
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
            self.path.unlink(missing_ok=True)

    def _read_pid(self) -> int | None:
        try:
            return int(self.path.read_text().strip() or 0) or None
        except (OSError, ValueError):
            return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
