#!/usr/bin/env python3
"""Разовый ручной вход в аккаунт tanki.su.

    login.py <имя>    завести аккаунт или перелогинить существующий
    login.py          показать уже заведённые

Открывает видимое окно Chromium на профиле этого аккаунта и ждёт, пока ты
залогинишься сам — через Lesta OpenID, с 2FA и «запомнить меня», как обычно.
Как только API страницы перестаёт отвечать «not logged in», окно закрывается,
а cookie остаются в профиле на диске. Дальше их использует checkin.py.

У каждого аккаунта свой профиль, так что сессии друг друга не затирают. Чтобы
войти под другим аккаунтом, просто возьми другое имя.
"""

from __future__ import annotations

import sys
import time

import platform_bits
from checkin import (
    ENTITLEMENTS_URL,
    JS_REQUEST,
    PAGE_URL,
    PROFILES_DIR,
    USER_AGENT,
    discover_accounts,
    entitlement_names,
    is_not_logged_in,
    month_token,
    profile_dir,
    valid_account_name,
)

WAIT_LIMIT_SEC = 15 * 60
POLL_PAUSE_SEC = 2


def logged_in(page) -> bool:
    """Проверяет сессию тем же запросом, что делает виджет календаря."""
    token = month_token(int(time.time() * 1000))
    try:
        resp = page.evaluate(
            JS_REQUEST,
            {"url": ENTITLEMENTS_URL, "body": {"entitlements": entitlement_names(token)}},
        )
    except Exception:
        # Страница в этот момент может переходить между шагами OpenID.
        return False
    return not is_not_logged_in(resp)


def show_accounts() -> int:
    accounts = discover_accounts()
    if accounts:
        print("Заведённые аккаунты:")
        for account in accounts:
            print(f"  {account}")
        print(f"\nДобавить ещё:  {platform_bits.how_to_run('login.py')} <имя>")
    else:
        print(f"Аккаунтов пока нет. Заведи первый:  {platform_bits.how_to_run('login.py')} <имя>")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        return show_accounts()

    account = sys.argv[1]
    if not valid_account_name(account):
        print(
            f"Имя «{account}» не годится: нужны буквы, цифры, дефис, точка или "
            "подчёркивание, до 32 символов, без косых черт.",
            file=sys.stderr,
        )
        return 2

    from playwright.sync_api import sync_playwright

    target = profile_dir(account)
    known = target.is_dir()
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Аккаунт: {account}{' (перелогин)' if known else ' (новый)'}")
    print(f"Профиль: {target}")
    print("Открываю окно. Войди в аккаунт на tanki.su — я подожду.\n")

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(target),
            headless=False,
            user_agent=USER_AGENT,
            viewport={"width": 1400, "height": 900},
            locale="ru-RU",
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60_000)

            deadline = time.time() + WAIT_LIMIT_SEC
            while time.time() < deadline:
                if logged_in(page):
                    print(f"\nГотово: сессия аккаунта «{account}» сохранена в профиле.")
                    checkin_cmd = platform_bits.how_to_run("checkin.py")
                    print(f"Проверить:  {checkin_cmd} --account {account} --dry-run -v")
                    return 0
                time.sleep(POLL_PAUSE_SEC)

            print("\nНе дождался входа за 15 минут. Запусти login.py ещё раз.")
            return 1
        finally:
            context.close()


if __name__ == "__main__":
    sys.exit(main())
