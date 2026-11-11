"""Командная строка: python -m realty_scraper <команда> [параметры]."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import db, pipeline
from .config import load_config
from .models import BlockedError

EXIT_OK, EXIT_ERROR, EXIT_PARTIAL, EXIT_BLOCKED = 0, 1, 2, 3


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="realty_scraper", description="Сбор объявлений о квартирах (Авито, СПб)")
    p.add_argument("--config", "-c", help="путь к config.yaml (по умолчанию ./config.yaml или $REALTY_CONFIG)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="создать папку данных и базу")
    sub.add_parser("probe", help="проверка на живом сайте: 1 страница выдачи, фильтр по цене, 1 карточка")

    sp = sub.add_parser("pilot", help="пилот: N свежих объявлений → карточки → фото → отчёт")
    sp.add_argument("-n", type=int, default=500)
    sp.add_argument("--no-photos", action="store_true")

    sp = sub.add_parser("daily", help="ежедневный запуск: вся выдача, новые карточки, проверка снятых, фото")
    sp.add_argument("--budget-hours", type=float, default=7.0, help="лимит времени запуска (0 — без лимита)")
    sp.add_argument("--no-photos", action="store_true")

    sp = sub.add_parser("search", help="только выдача")
    sp.add_argument("--max-cards", type=int)
    sp.add_argument("--no-slice", action="store_true", help="без нарезки по цене (не больше 100 страниц)")

    sp = sub.add_parser("items", help="только карточки")
    sp.add_argument("--mode", choices=["new", "refresh", "verify"], default="new")
    sp.add_argument("--limit", type=int)

    sp = sub.add_parser("photos", help="только скачивание фото")
    sp.add_argument("--limit", type=int)

    sub.add_parser("reparse", help="повторно разобрать сохранённые страницы карточек")
    sub.add_parser("stats", help="сводка по базе, запускам и ошибкам")
    sub.add_parser("report", help="отчёт по всей базе: заполненность полей, фото, скорость, прогноз объёма")
    sp = sub.add_parser("backup", help="резервная копия базы в data/backup")
    sp.add_argument("--keep", type=int, default=14)
    sp = sub.add_parser("export", help="выгрузить listings.csv и photos.csv")
    sp.add_argument("--out", default=None, help="папка (по умолчанию data/export)")

    args = p.parse_args(argv)
    cfg = load_config(args.config)

    try:
        if args.cmd == "init":
            cfg.data_path.mkdir(parents=True, exist_ok=True)
            db.connect(cfg.db_path).close()
            print(f"База создана: {cfg.db_path}")
            return EXIT_OK
        if args.cmd == "stats":
            pipeline.print_stats(cfg)
            return EXIT_OK
        if args.cmd == "report":
            pipeline.write_full_report(cfg)
            return EXIT_OK
        if args.cmd == "backup":
            pipeline.backup_db(cfg, keep=args.keep)
            return EXIT_OK
        if args.cmd == "export":
            pipeline.export_csv(cfg, Path(args.out) if args.out else cfg.path("export"))
            return EXIT_OK
        if args.cmd == "probe":
            pipeline.run_probe(cfg)
        elif args.cmd == "pilot":
            pipeline.run_pilot(cfg, n=args.n, photos=not args.no_photos)
        elif args.cmd == "daily":
            pipeline.run_daily(cfg, budget_hours=args.budget_hours or None, photos=not args.no_photos)
        elif args.cmd == "search":
            pipeline.run_search(cfg, args.max_cards, sliced=not args.no_slice)
        elif args.cmd == "items":
            pipeline.run_items(cfg, args.mode, args.limit)
        elif args.cmd == "photos":
            pipeline.run_photos(cfg, args.limit)
        elif args.cmd == "reparse":
            pipeline.run_reparse(cfg)
    except BlockedError:
        return EXIT_BLOCKED
    except RuntimeError as e:  # например, уже идёт другой запуск
        print(e, file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        return EXIT_ERROR
    except Exception:  # noqa: BLE001 — подробности уже в журнале
        return EXIT_ERROR
    status = _last_status(cfg)
    return {"ok": EXIT_OK, "partial": EXIT_PARTIAL, "blocked": EXIT_BLOCKED}.get(status, EXIT_ERROR)


def _last_status(cfg) -> str | None:
    con = db.connect(cfg.db_path)
    row = con.execute("SELECT status FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
    con.close()
    return row["status"] if row else None


if __name__ == "__main__":
    sys.exit(main())
