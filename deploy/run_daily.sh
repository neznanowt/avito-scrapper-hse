#!/usr/bin/env bash
# Ежедневный сбор + резервная копия базы. Запускается по расписанию (cron или systemd timer).
# Коды выхода: 0 — успех, 2 — не уложились в лимит времени (продолжит завтра), 3 — блокировка, 1 — ошибка.
set -uo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate
export REALTY_CONFIG="${REALTY_CONFIG:-$PWD/config.yaml}"

# Авито хуже относится к headless-браузеру, поэтому на сервере без экрана — виртуальный дисплей.
if [ -z "${DISPLAY:-}" ] && command -v xvfb-run >/dev/null; then
  xvfb-run -a --server-args="-screen 0 1366x900x24" python -m realty_scraper daily --budget-hours 7
else
  python -m realty_scraper daily --budget-hours 7
fi
code=$?
python -m realty_scraper backup --keep 14
exit $code
