"""Всё, чем Windows отличается от Linux, — в одном месте.

Логика забора наград одинакова везде, различаются только четыре вещи: куда
класть данные, как взять блокировку, чем показать уведомление и каким
User-Agent представляться. Держим их здесь, чтобы `checkin.py` оставался
про табель, а не про операционные системы.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("tanki-checkin")

IS_WINDOWS = sys.platform.startswith("win")

# Сессия может быть привязана к User-Agent, поэтому он должен соответствовать
# системе, с которой заходят: линуксовый UA на Windows-машине — лишний повод
# для сайта переспросить.
USER_AGENT_LINUX = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
USER_AGENT_WINDOWS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def user_agent() -> str:
    return USER_AGENT_WINDOWS if IS_WINDOWS else USER_AGENT_LINUX


def how_to_run(script: str) -> str:
    """Как позвать скрипт в подсказках: у Windows нет шебанга."""
    return f"python {script}" if IS_WINDOWS else f"./{script}"


def _windows_local_appdata() -> Path:
    """`%LOCALAPPDATA%`, а если переменной нет — обычное для неё место."""
    raw = os.environ.get("LOCALAPPDATA")
    return Path(raw) if raw else Path.home() / "AppData" / "Local"


def data_dir() -> Path:
    """Профили Chromium: то, что переживает переустановку скрипта."""
    override = os.environ.get("TANKI_CHECKIN_DATA")
    if override:
        return Path(override)
    if IS_WINDOWS:
        return _windows_local_appdata() / "tanki-checkin"
    return Path.home() / ".local/share/tanki-checkin"


def state_dir() -> Path:
    """Лог, замок и даты последнего забора: рабочее состояние."""
    override = os.environ.get("TANKI_CHECKIN_STATE")
    if override:
        return Path(override)
    if IS_WINDOWS:
        # На Windows нет отдельного места под state — кладём рядом с данными.
        return _windows_local_appdata() / "tanki-checkin" / "state"
    return Path.home() / ".local/state/tanki-checkin"


def lock_exclusive(handle: int) -> bool:
    """Занять файл-замок без ожидания. False — замок уже держит другой прогон.

    Windows и Linux блокируют файлы по-разному, но нам нужно одно: попробовать
    и сразу сказать, вышло или нет.
    """
    if IS_WINDOWS:
        import msvcrt

        try:
            msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    import fcntl

    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


# Тост показывается через PowerShell, чтобы не тянуть зависимостей. Текст
# подставляется не в строку скрипта, а через переменные окружения: иначе
# кавычка в тексте ломала бы скрипт.
_TOAST_PS1 = (
    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,"
    " ContentType=WindowsRuntime] > $null;"
    "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(2);"
    "$n = $t.GetElementsByTagName('text');"
    "$n.Item(0).AppendChild($t.CreateTextNode($env:TANKI_TOAST_TITLE)) > $null;"
    "$n.Item(1).AppendChild($t.CreateTextNode($env:TANKI_TOAST_BODY)) > $null;"
    "$toast = [Windows.UI.Notifications.ToastNotification]::new($t);"
    "[Windows.UI.Notifications.ToastNotificationManager]"
    "::CreateToastNotifier('Табель Мира танков').Show($toast);"
)


def notify(title: str, body: str, urgency: str = "critical") -> None:
    """Показать уведомление. Не вышло — это не повод ронять прогон."""
    try:
        if IS_WINDOWS:
            environment = dict(os.environ)
            environment["TANKI_TOAST_TITLE"] = title
            environment["TANKI_TOAST_BODY"] = body
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", _TOAST_PS1],
                timeout=20,
                check=False,
                env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            subprocess.run(
                ["notify-send", "-u", urgency, "-a", "Табель Мира танков", title, body],
                timeout=10,
                check=False,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("уведомление не показалось: %s", exc)
