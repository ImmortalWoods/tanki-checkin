@echo off
chcp 65001 >nul
rem Заводит две задачи в планировщике: ежедневно в 09:00 и при входе в систему.
rem Аналог systemd-таймера и запуска по graphical-session.target на Linux.
setlocal

set "PROJECT=%~dp0.."
for %%p in ("%PROJECT%") do set "PROJECT=%%~fp"
set "SCRIPT=%PROJECT%\checkin.py"

rem Задача «при входе в систему» регистрируется на уровне системы, поэтому
rem планировщик требует повышенных прав. Проверяем сразу, а не на полпути.
net session >nul 2>&1
if errorlevel 1 (
    echo Нужны права администратора.
    echo Закрой это окно, кликни по install.cmd правой кнопкой
    echo и выбери "Запуск от имени администратора".
    echo.
    pause
    exit /b 1
)

if not exist "%SCRIPT%" (
    echo Не нашёл "%SCRIPT%" — запускай install.cmd из каталога windows проекта.
    exit /b 1
)

rem pythonw, а не python: задача работает без окна консоли. Лог всё равно
rem пишется в файл, а о проблемах приходит уведомление.
set "PYW="
for /f "delims=" %%p in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%p"

if not defined PYW (
    echo Не найден pythonw. Поставь Python с python.org и отметь "Add to PATH".
    exit /b 1
)

echo Интерпретатор: %PYW%
echo Проект:        %PROJECT%
echo.

schtasks /create /f /tn "TankiCheckin" /tr "\"%PYW%\" \"%SCRIPT%\"" /sc daily /st 09:00 /rl limited
if errorlevel 1 goto failed

schtasks /create /f /tn "TankiCheckin-Logon" /tr "\"%PYW%\" \"%SCRIPT%\"" /sc onlogon /rl limited
if errorlevel 1 goto failed

echo.
echo Готово. Задачи "TankiCheckin" и "TankiCheckin-Logon" созданы.
echo Проверить:  schtasks /query /tn TankiCheckin
echo Забрать сейчас:  schtasks /run /tn TankiCheckin
exit /b 0

:failed
echo.
echo Планировщик отказал. Запусти install.cmd от имени администратора.
exit /b 1
