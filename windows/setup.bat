@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."
echo.
echo === Установка сборщика Авито ===
echo.

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY goto nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul || goto nopython

if exist ".venv\Scripts\python.exe" goto venv_ready
echo [1/3] Создаю окружение Python в папке .venv ...
%PY% -m venv .venv || goto fail
:venv_ready

echo [2/3] Устанавливаю пакеты, это 1-3 минуты ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || goto fail

echo [3/3] Настройка ...
".venv\Scripts\python.exe" -m realty_scraper setup || goto fail
echo Установка завершена. Запуск сборщика: windows\run.bat
pause
exit /b 0

:nopython
echo Не найден Python 3.10 или новее.
echo Скачайте его с https://www.python.org/downloads/ и при установке отметьте галочку "Add python.exe to PATH".
echo Затем запустите этот файл снова.
pause
exit /b 1

:fail
echo.
echo Установка не удалась. Пришлите текст выше в чат.
pause
exit /b 1
