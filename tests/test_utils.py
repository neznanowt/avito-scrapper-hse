from datetime import datetime

from realty_scraper.utils import (
    MSK, parse_distance_m, parse_float, parse_floor, parse_int, parse_ru_date, parse_walk_minutes, seller_key,
)


def test_numbers():
    assert parse_float("50,4 м²") == 50.4
    assert parse_int("23 500 000 ₽") == 23_500_000
    assert parse_int("23 500 000") == 23_500_000
    assert parse_float("2.7 м") == 2.7
    assert parse_int("№ 8380391827") == 8380391827
    assert parse_int("от 643 000 ₽ за 50 м²") == 643_000
    assert parse_float(None) is None and parse_float("нет") is None


def test_floor_metro_distance():
    assert parse_floor("1 из 7") == (1, 7)
    assert parse_floor("12") == (12, None)
    assert parse_walk_minutes("6–10 мин.") == (6, 10)
    assert parse_walk_minutes("до 5 мин.") == (0, 5)
    assert parse_walk_minutes("от 31 мин.") == (31, None)
    assert parse_distance_m(" 500 м") == 500
    assert parse_distance_m(" 1,6 км") == 1600


def test_ru_dates():
    now = datetime(2026, 11, 11, 15, 0, tzinfo=MSK)
    assert parse_ru_date("сегодня в 14:04", now) == datetime(2026, 11, 11, 14, 4, tzinfo=MSK)
    assert parse_ru_date(" · вчера в 9:15", now) == datetime(2026, 11, 10, 9, 15, tzinfo=MSK)
    assert parse_ru_date("3 ноября в 12:00", now) == datetime(2026, 11, 3, 12, 0, tzinfo=MSK)
    assert parse_ru_date("25 декабря в 10:00", now).year == 2025  # без года и в будущем → прошлый год
    assert parse_ru_date("5 марта 2025", now) == datetime(2025, 3, 5, tzinfo=MSK)
    assert parse_ru_date("непонятно", now) is None


def test_seller_key_is_stable_and_salted():
    a = seller_key("s1", "avito", 123)
    assert a == seller_key("s1", "avito", 123) and a != seller_key("s2", "avito", 123)
    assert seller_key("s1", "avito", None) is None
