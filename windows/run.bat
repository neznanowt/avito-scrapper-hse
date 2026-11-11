@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."
set "PYEXE=.venv\Scripts\python.exe"
if not exist "%PYEXE%" goto nosetup
if not exist "config.yaml" goto nosetup
if not "%~1"=="" goto direct

:menu
echo.
echo ================== Сборщик Авито ==================
echo  1  Проверка: 3 страницы Авито, в базу не пишет
echo  2  Пилот: 500 свежих объявлений, карточки, фото, отчёт
echo  3  Полный сбор: вся выдача, новые карточки, фото; до 7 часов
echo  4  Дособрать карточки и фото после остановки
echo  5  Отчёт по базе
echo  6  Сводка: запуски и ошибки
echo  7  Открыть окно Chrome сборщика
echo  8  Обновить код с GitHub
echo  0  Выход
echo ===================================================
set "N="
set /p "N=Номер пункта и Enter: "
if "%N%"=="1" call :rs probe
if "%N%"=="2" call :rs pilot -n 500
if "%N%"=="3" call :rs daily --budget-hours 7
if "%N%"=="4" call :rs items --mode new
if "%N%"=="4" call :rs photos
if "%N%"=="5" call :rs report
if "%N%"=="6" call :rs stats
if "%N%"=="7" call :rs chrome
if "%N%"=="8" ("%PYEXE%" -m realty_scraper update & pause & exit /b)
if "%N%"=="0" exit /b 0
goto menu

:rs
echo.
"%PYEXE%" -m realty_scraper %*
echo.
echo Готово, код выхода %errorlevel%: 0 - успешно, 2 - не уложились во время, 3 - блокировка Авито.
pause
exit /b

:direct
"%PYEXE%" -m realty_scraper %*
exit /b %errorlevel%

:nosetup
echo Сначала запустите windows\setup.bat
pause
exit /b 1
