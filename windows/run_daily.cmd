@echo off
chcp 65001 >nul
rem Ручной запуск забора. Планировщик зовёт checkin.py напрямую, эта обёртка —
rem для того, чтобы запустить прогон руками и увидеть вывод.
setlocal

set "PROJECT=%~dp0.."
for %%p in ("%PROJECT%") do set "PROJECT=%%~fp"

if not defined TANKI_CHECKIN_PYTHON set "TANKI_CHECKIN_PYTHON=python"

"%TANKI_CHECKIN_PYTHON%" "%PROJECT%\checkin.py" %*
exit /b %ERRORLEVEL%
