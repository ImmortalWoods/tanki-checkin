@echo off
chcp 65001 >nul
rem Убирает задачи планировщика. Профили и лог остаются на месте.
setlocal

schtasks /delete /f /tn "TankiCheckin" 2>nul
schtasks /delete /f /tn "TankiCheckin-Logon" 2>nul

echo Задачи удалены. Данные остались в %%LOCALAPPDATA%%\tanki-checkin.
exit /b 0
