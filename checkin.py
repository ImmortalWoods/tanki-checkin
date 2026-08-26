#!/usr/bin/env python3
"""Автозабор ежедневных наград «табеля» на tanki.su, по всем аккаунтам сразу.

Аккаунт — это отдельный персистентный профиль Chromium в profiles/<имя>.
Реестра аккаунтов нет: список подкаталогов и есть реестр, поэтому он не может
разъехаться с реальностью. Прогон последовательный, сбой на одном аккаунте не
мешает остальным.

Повторяет последовательность запросов, которую делает виджет календаря, из
контекста самой страницы — так cookie, Origin и Referer правильные без ручной
подделки заголовков. Логика выбора дня живёт в чистых функциях ниже и покрыта
тестами в test_logic.py.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import platform_bits

PAGE_URL = "https://tanki.su/ru/daily-check-in/"
BASE = "https://tanki.su"
TIMESTAMP_URL = f"{BASE}/timestamp/"
ENTITLEMENTS_URL = f"{BASE}/mtup/claim_product/get_inventory_entitlements/"
PRODUCTS_URL = f"{BASE}/mtup/claim_product/get_products_list/"
PURCHASE_URL = f"{BASE}/mtup/claim_product/purchase_product_vc/"
LANGUAGE = "ru"
# Кука портала с игровым id аккаунта.
PORTAL_ACCOUNT_ID_COOKIE = "cm.options.user_id"

# Тот же UA держим и в login.py, и здесь: сессия может быть привязана к нему,
# а headless Chromium по умолчанию представляется как HeadlessChrome.
USER_AGENT = platform_bits.user_agent()

DATA_DIR = platform_bits.data_dir()
STATE_DIR = platform_bits.state_dir()
PROFILES_DIR = DATA_DIR / "profiles"
CLAIM_STATE_DIR = STATE_DIR / "state"
LOG_FILE = STATE_DIR / "log"
LOCK_FILE = STATE_DIR / "lock"

# Пометка в каталоге профиля временно исключает аккаунт из прогона.
DISABLED_MARKER = ".disabled"

# Столько дней подряд без единого забора считаем поводом присмотреться.
# Страховка от самого неприятного отказа: если Lesta поменяет формат кодов,
# скрипт будет молча отвечать «забирать нечего» и награды тихо перестанут идти.
STALE_AFTER_DAYS = 3

NETWORK_RETRIES = 3
RETRY_PAUSE_SEC = 60
# Небольшая пауза между аккаунтами, чтобы не долбить сайт очередью запросов.
ACCOUNT_PAUSE_SEC = 5

EXIT_OK = 0
EXIT_NEED_LOGIN = 2
EXIT_API_CHANGED = 3
EXIT_NETWORK = 4

log = logging.getLogger("tanki-checkin")


# --------------------------------------------------------------------------
# Чистая логика. Ничего не знает ни о браузере, ни о сети, ни о файлах.
# --------------------------------------------------------------------------

MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")

DAYS_IN_CALENDAR = 31

# Догадка: точный текст ответа «уже забрано» без залогиненного аккаунта не увидеть.
# Стоит второй линией после проверки счётчика и всегда пишет сырой ответ в лог.
ALREADY_CLAIMED_MARKERS = ("already", "уже получен", "уже забран", "duplicate")

# Имя аккаунта становится именем каталога, поэтому разделители и служебные
# имена не годятся. Буквы любого алфавита разрешены: имена для человека.
ACCOUNT_NAME_RE = re.compile(r"^[^\W]{1}[\w.-]{0,31}$", re.UNICODE)

KIND_CLAIMED = "claimed"
KIND_NOOP = "noop"
KIND_NEED_LOGIN = "need_login"
KIND_API_ERROR = "api_error"
KIND_NETWORK_ERROR = "network_error"


class ApiError(RuntimeError):
    """Сервер ответил не тем, чего ждёт скрипт."""


class NeedLogin(RuntimeError):
    """Сессия протухла, нужен повторный ручной вход."""


@dataclass
class ClaimResult:
    """Что удалось узнать за один проход по календарю."""

    claimed: bool
    message: str
    token: str = ""
    counter: int = 0
    code: str | None = None
    account_id: str | None = None


@dataclass
class Outcome:
    """Чем закончился прогон по одному аккаунту."""

    account: str
    kind: str
    message: str
    stale: str | None = None
    code: str | None = None
    undelivered: str | None = None


def valid_account_name(name: str) -> bool:
    return bool(name) and name not in {".", ".."} and bool(ACCOUNT_NAME_RE.match(name))


def month_token(ts_ms: int) -> str:
    """Токен месяца по серверному времени, как в бандле виджета.

    JS-оригинал: C[d.getUTCMonth()] + String(d.getUTCFullYear()).slice(2)
    """
    moment = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return f"{MONTHS[moment.month - 1]}{moment.year % 100:02d}"


def counter_name(token: str) -> str:
    return f"{token}ru_counter_claim"


def entitlement_names(token: str) -> list[str]:
    return [counter_name(token), "daily_claim", "daily_complete"]


def product_codes(token: str) -> list[str]:
    return [f"claim_{token}_{day}" for day in range(1, DAYS_IN_CALENDAR + 1)]


def response_errors(resp: dict) -> list[str]:
    """Список сообщений об ошибке из конверта ответа, если он там есть."""
    data = resp.get("data")
    if not isinstance(data, dict):
        return []
    errors = data.get("errors")
    if isinstance(errors, list):
        return [str(item) for item in errors]
    return [str(errors)] if errors else []


def is_not_logged_in(resp: dict) -> bool:
    if resp.get("status") == 403:
        return True
    return any("not logged in" in message.lower() for message in response_errors(resp))


def is_already_claimed(resp: dict) -> bool:
    haystack = " ".join(response_errors(resp)).lower()
    return any(marker in haystack for marker in ALREADY_CLAIMED_MARKERS)


def parse_timestamp_ms(resp: dict) -> int:
    """/timestamp/ отдаёт голое число — секунды epoch."""
    data = resp.get("data")
    if isinstance(data, bool) or not isinstance(data, (int, float)):
        raise ApiError(f"/timestamp/ вернул не число: {short(resp)}")
    return int(data * 1000)


def require_ok(resp: dict, what: str) -> dict:
    """Проверяет конверт {"status":"ok","data":…} и возвращает полезную нагрузку."""
    if is_not_logged_in(resp):
        raise NeedLogin(f"{what}: сессия не авторизована")
    data = resp.get("data")
    if not isinstance(data, dict):
        raise ApiError(f"{what}: ожидался JSON-объект, пришло {short(resp)}")
    if data.get("status") != "ok":
        raise ApiError(f"{what}: status != ok, ответ {short(resp)}")
    return data


def amounts_from_entitlements(payload: dict) -> dict[str, int]:
    """[{"code": …, "amount": …}, …] → {code: amount}."""
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise ApiError(f"энтайтлменты: ожидался список, пришло {json.dumps(rows)[:300]}")
    amounts: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict) or "code" not in row:
            continue
        try:
            amounts[str(row["code"])] = int(row.get("amount") or 0)
        except (TypeError, ValueError):
            amounts[str(row["code"])] = 0
    return amounts


def available_codes(payload: dict) -> set[str]:
    items = (payload.get("data") or {}).get("items")
    if not isinstance(items, list):
        raise ApiError(f"список продуктов: нет items, пришло {json.dumps(payload)[:300]}")
    return {str(item.get("product_code")) for item in items if isinstance(item, dict)}


def already_claimed_today(token: str, amounts: dict[str, int]) -> str | None:
    """Условие виджета «сегодняшний день уже пройден», иначе None.

    Проверено на живом аккаунте: сразу после забора энтайтлменты становятся
    ровно {'daily_claim': 1}, поэтому одной этой метки достаточно.
    """
    if amounts.get("daily_claim", 0) and not amounts.get("daily_complete", 0):
        return f"сегодня уже забрано (счётчик {amounts.get(counter_name(token), 0)})"
    return None


def day_of(code: str) -> int:
    """Номер дня из кода вида claim_aug26_7. Неразбираемое уходит в конец."""
    tail = code.rsplit("_", 1)[-1]
    return int(tail) if tail.isdigit() else DAYS_IN_CALENDAR + 1


def preferred_code(token: str, amounts: dict[str, int]) -> str | None:
    """Код, на который указывает счётчик забранных дней."""
    day = amounts.get(counter_name(token), 0) + 1
    return f"claim_{token}_{day}" if day <= DAYS_IN_CALENDAR else None


def choose_code(token: str, amounts: dict[str, int], offered: set[str]) -> tuple[str | None, str]:
    """Что забирать: счётчик — предпочтение, список сервера — источник истины.

    Счётчик обновляется не мгновенно (наблюдение 2026-08-03: сразу после забора
    он ещё нулевой), поэтому одной арифметике по нему доверять нельзя — иначе
    при расхождении скрипт вечно целился бы в уже купленный код и молча встал.
    Сервер же прямо перечисляет, что сейчас доступно.
    """
    preferred = preferred_code(token, amounts)
    counter = amounts.get(counter_name(token), 0)

    if preferred and preferred in offered:
        return preferred, f"счётчик {counter} → день {day_of(preferred)}"

    if not offered:
        return None, "сервер не предлагает сегодня ни одного кода"

    fallback = min(offered, key=day_of)
    return fallback, (
        f"счётчик {counter} указывал на {preferred or '—'}, но сервер предлагает "
        f"{sorted(offered, key=day_of)} — беру {fallback}"
    )


def purchase_body(code: str) -> dict:
    return {
        "product_code": code,
        "language": LANGUAGE,
        "transaction_id": str(uuid.uuid4()),
        "expected_prices": [{"code": "gold", "amount": "0", "item_type": "currency"}],
    }


def delivery_check(state: dict, counter_now: int, today: str) -> tuple[str | None, bool]:
    """Доставлена ли награда, забранная в прошлый раз.

    По правилам календаря забор на сайте — половина сделки: если до 3:00 МСК
    следующего дня не зайти в игровой клиент, награда не доставляется, а
    следующий день не открывается. Признак доставки — сдвиг счётчика: он
    считает именно доставленные дни.

    Возвращает (предупреждение или None, надо ли забыть про эту награду).
    Предупреждаем один раз: если в клиент так и не заходят, дальше об этом
    скажет страховка от простоя, а не ежедневное нытьё.
    """
    pending = state.get("pending")
    if not isinstance(pending, dict):
        return None, False

    if counter_now > pending.get("counter_before", 0):
        return None, True

    if pending.get("date") == today:
        # Забрали сегодня, дедлайн ещё не наступил.
        return None, False

    return (
        f"награда {pending.get('code')} за {pending.get('date')} не доставлена: "
        "в клиент не заходили до 3:00 МСК"
    ), True


def days_without_claim(state: dict, today: str) -> int:
    """Сколько дней прошло без успешного забора.

    Отсчёт от последнего забора, а если его ещё не было — от первого запуска,
    чтобы свежая установка не начинала с жалобы.
    """
    reference = state.get("last_claim") or state.get("since")
    if not reference:
        return 0
    try:
        start = date.fromisoformat(reference)
        now = date.fromisoformat(today)
    except ValueError:
        return 0
    return max(0, (now - start).days)


def stale_warning(state: dict, today: str) -> str | None:
    idle = days_without_claim(state, today)
    if idle <= STALE_AFTER_DAYS:
        return None
    last = state.get("last_claim")
    tail = f"последний забор {last}" if last else "ни одного забора с момента установки"
    return f"{idle} дней без наград: {tail}"


def summarize(outcomes: list[Outcome]) -> tuple[int, str | None, str | None]:
    """Сводка по всем аккаунтам → (код возврата, заголовок и текст уведомления).

    Заголовок и текст равны None, когда сообщать не о чем. Приоритет кода
    возврата: смена API важнее протухшей сессии, та важнее сетевого сбоя, потому
    что первое ломает вообще всё и чинится только правкой скрипта.
    """
    by_kind: dict[str, list[str]] = {}
    for outcome in outcomes:
        by_kind.setdefault(outcome.kind, []).append(outcome.account)
    stale = [outcome.account for outcome in outcomes if outcome.stale]
    undelivered = [outcome for outcome in outcomes if outcome.undelivered]
    claimed = [outcome for outcome in outcomes if outcome.kind == KIND_CLAIMED]

    lines: list[str] = []
    if names := by_kind.get(KIND_API_ERROR):
        lines.append(f"сервер ответил неожиданно: {', '.join(names)}")
    if names := by_kind.get(KIND_NEED_LOGIN):
        lines.append(f"нужен повторный вход: {', '.join(names)}")
    if names := by_kind.get(KIND_NETWORK_ERROR):
        lines.append(f"не удалось достучаться: {', '.join(names)}")
    if undelivered:
        lines.append(
            "не доставлено, награда пропала: "
            + ", ".join(f"{item.account} ({item.undelivered})" for item in undelivered)
        )
    if claimed:
        names = ", ".join(
            f"{item.account} (день {day_of(item.code)})" if item.code else item.account
            for item in claimed
        )
        lines.append(
            f"забрано: {names}.\nЗайди в клиент до 3:00 МСК, иначе не доставится."
        )
    if stale:
        lines.append(f"давно нет наград: {', '.join(stale)}")

    if KIND_API_ERROR in by_kind:
        code, title = EXIT_API_CHANGED, "Табель: сервер ответил неожиданно"
    elif KIND_NEED_LOGIN in by_kind:
        code, title = EXIT_NEED_LOGIN, "Табель: нужен повторный вход"
    elif KIND_NETWORK_ERROR in by_kind:
        code, title = EXIT_NETWORK, "Табель: не удалось достучаться до tanki.su"
    elif claimed:
        # Забор без захода в клиент пропадает, поэтому успех тоже требует действия.
        code, title = EXIT_OK, "Табель: зайди в клиент"
    elif undelivered:
        code, title = EXIT_OK, "Табель: награда не доставлена"
    elif stale:
        code, title = EXIT_OK, "Табель: награды давно не приходят"
    else:
        return EXIT_OK, None, None

    return code, title, "\n".join(lines)


def short(resp: dict, limit: int = 600) -> str:
    """Компактное представление ответа для лога."""
    body = resp.get("text")
    if body is None:
        body = json.dumps(resp.get("data"), ensure_ascii=False)
    return f"HTTP {resp.get('status')} {str(body)[:limit]}"


# --------------------------------------------------------------------------
# Аккаунты и состояние на диске
# --------------------------------------------------------------------------


class AccountLog(logging.LoggerAdapter):
    """Помечает каждую строку лога именем аккаунта."""

    def process(self, msg, kwargs):
        return f"[{self.extra['account']}] {msg}", kwargs


def profile_dir(account: str) -> Path:
    return PROFILES_DIR / account


def claim_state_file(account: str) -> Path:
    return CLAIM_STATE_DIR / f"{account}.json"


def discover_accounts() -> list[str]:
    """Реестр аккаунтов — это подкаталоги profiles/, ничего больше."""
    if not PROFILES_DIR.is_dir():
        return []
    found = []
    for entry in sorted(PROFILES_DIR.iterdir()):
        if not entry.is_dir() or not valid_account_name(entry.name):
            continue
        if (entry / DISABLED_MARKER).exists():
            continue
        found.append(entry.name)
    return found


def load_claim_state(account: str, today: str) -> dict:
    try:
        state = json.loads(claim_state_file(account).read_text(encoding="utf-8"))
        if isinstance(state, dict):
            return state
    except (OSError, ValueError):
        pass
    return {"since": today, "last_claim": None}


def save_claim_state(account: str, state: dict) -> None:
    try:
        CLAIM_STATE_DIR.mkdir(parents=True, exist_ok=True)
        claim_state_file(account).write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as exc:
        log.warning("не удалось записать %s: %s", claim_state_file(account), exc)


# --------------------------------------------------------------------------
# Браузер
# --------------------------------------------------------------------------

# Один универсальный помощник: всё остальное решается на стороне Python.
JS_REQUEST = """
async ({url, body}) => {
  const init = {headers: {'Content-Type': 'application/json'}, credentials: 'include'};
  if (body !== null) { init.method = 'POST'; init.body = JSON.stringify(body); }
  const response = await fetch(url, init);
  const text = await response.text();
  let data = null;
  try { data = JSON.parse(text); } catch (e) { data = null; }
  return {status: response.status, data, text: text.slice(0, 4000)};
}
"""


def api(page, url: str, body: dict | None, alog) -> dict:
    resp = page.evaluate(JS_REQUEST, {"url": url, "body": body})
    alog.debug("%s → %s", url, short(resp, 300))
    return resp


def claim(page, dry_run: bool, alog) -> ClaimResult:
    """Один проход по календарю."""
    ts_resp = api(page, TIMESTAMP_URL, None, alog)
    token = month_token(parse_timestamp_ms(ts_resp))
    alog.info("токен месяца: %s", token)

    entitlements = require_ok(
        api(page, ENTITLEMENTS_URL, {"entitlements": entitlement_names(token)}, alog),
        "энтайтлменты",
    )
    amounts = amounts_from_entitlements(entitlements)
    alog.info("энтайтлменты: %s", amounts)
    counter = amounts.get(counter_name(token), 0)

    if reason := already_claimed_today(token, amounts):
        return ClaimResult(False, f"забирать нечего: {reason}", token, counter)

    products_resp = api(
        page,
        PRODUCTS_URL,
        {
            "product_codes": product_codes(token),
            "language": LANGUAGE,
            "etag": int(time.time() * 1000),
        },
        alog,
    )
    offered = available_codes(require_ok(products_resp, "список продуктов"))
    code, reason = choose_code(token, amounts, offered)

    if code is None:
        # Пустой список неотличим от смены формата кодов, поэтому сырой ответ
        # уходит в лог всегда — по нему потом и разбираться.
        alog.info("ответ списка продуктов: %s", short(products_resp, 2000))
        return ClaimResult(False, f"забирать нечего: {reason}", token, counter)

    if code != preferred_code(token, amounts):
        # Модель счётчика разошлась с сервером — стоит знать, даже если забор прошёл.
        alog.warning("расхождение со счётчиком: %s", reason)
    alog.info("цель: %s — %s", code, reason)

    if dry_run:
        return ClaimResult(
            False, f"DRY RUN: забрал бы {code}, запрос не отправлен", token, counter, code
        )

    resp = api(page, PURCHASE_URL, purchase_body(code), alog)
    if is_not_logged_in(resp):
        raise NeedLogin("покупка: сессия не авторизована")

    data = resp.get("data")
    if isinstance(data, dict) and data.get("status") == "ok":
        alog.info("ответ на забор: %s", short(resp, 2000))
        return ClaimResult(True, f"забрано: {code}", token, counter, code)

    if is_already_claimed(resp):
        alog.warning("сервер сказал «уже забрано», сырой ответ: %s", short(resp, 2000))
        return ClaimResult(
            False, f"забирать нечего: {code} уже забран по версии сервера", token, counter
        )

    raise ApiError(f"покупка {code} не удалась, ответ {short(resp, 2000)}")


def portal_account_id(context) -> str | None:
    """Игровой id аккаунта из куки портала."""
    for cookie in context.cookies():
        if cookie.get("name") == PORTAL_ACCOUNT_ID_COOKIE:
            value = str(cookie.get("value") or "").strip()
            return value or None
    return None


def visit(playwright, account: str, dry_run: bool, headless: bool, alog) -> ClaimResult:
    context = playwright.chromium.launch_persistent_context(
        str(profile_dir(account)),
        headless=headless,
        user_agent=USER_AGENT,
        viewport={"width": 1400, "height": 900},
        locale="ru-RU",
    )
    try:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60_000)
        result = claim(page, dry_run, alog)
        result.account_id = portal_account_id(context)
        return result
    finally:
        context.close()


def process_account(playwright, account: str, dry_run: bool, headless: bool, today: str) -> Outcome:
    """Прогон по одному аккаунту. Исключения наружу не выпускает."""
    from playwright.sync_api import Error as PlaywrightError

    alog = AccountLog(log, {"account": account})
    state = load_claim_state(account, today)
    state.setdefault("since", today)

    last_network_error: Exception | None = None
    for attempt in range(1, NETWORK_RETRIES + 1):
        try:
            result = visit(playwright, account, dry_run, headless, alog)
            alog.info(result.message)

            if dry_run:
                return Outcome(account, KIND_NOOP, result.message)

            # Проверяем доставку до того, как записать сегодняшний забор:
            # счётчик прочитан в начале прогона, то есть ещё «до».
            undelivered, forget = delivery_check(state, result.counter, today)
            if undelivered:
                alog.warning(undelivered)
            if forget:
                state.pop("pending", None)

            if result.account_id:
                state["account_id"] = result.account_id

            if result.claimed:
                state["last_claim"] = today
                state["pending"] = {
                    "date": today,
                    "code": result.code,
                    "counter_before": result.counter,
                }
            state["last_run"] = today
            save_claim_state(account, state)

            warning = stale_warning(state, today)
            if warning:
                alog.warning(warning)
            return Outcome(
                account,
                KIND_CLAIMED if result.claimed else KIND_NOOP,
                result.message,
                stale=warning,
                code=result.code,
                undelivered=undelivered,
            )

        except NeedLogin as exc:
            alog.error("%s", exc)
            return Outcome(account, KIND_NEED_LOGIN, str(exc))

        except ApiError as exc:
            alog.error("%s", exc)
            return Outcome(account, KIND_API_ERROR, str(exc))

        except PlaywrightError as exc:
            last_network_error = exc
            alog.warning("попытка %d из %d не удалась: %s", attempt, NETWORK_RETRIES, exc)
            if attempt < NETWORK_RETRIES:
                time.sleep(RETRY_PAUSE_SEC)

    alog.error("сеть или браузер недоступны: %s", last_network_error)
    return Outcome(account, KIND_NETWORK_ERROR, str(last_network_error))


# --------------------------------------------------------------------------
# Обвязка
# --------------------------------------------------------------------------


def notify(title: str, body: str, urgency: str = "critical") -> None:
    platform_bits.notify(title, body, urgency)


def setup_logging(verbose: bool) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)

    to_file = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    to_file.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    log.addHandler(to_file)

    # systemd подхватывает stderr в журнал юнита; в Windows это окно консоли.
    to_stderr = logging.StreamHandler(sys.stderr)
    to_stderr.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    log.addHandler(to_stderr)


def acquire_lock():
    """Не даёт запуску по логину и запуску по расписанию наложиться."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    handle = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
    if not platform_bits.lock_exclusive(handle):
        os.close(handle)
        return None
    return handle


def print_accounts(today: str) -> int:
    accounts = discover_accounts()
    if not accounts:
        print(f"Аккаунтов нет. Заведи первый:  {platform_bits.how_to_run('login.py')} <имя>")
        return 1
    print(f"{'аккаунт':<20} {'последний забор':<18} простой")
    for account in accounts:
        state = load_claim_state(account, today)
        last = state.get("last_claim") or "—"
        print(f"{account:<20} {last:<18} {days_without_claim(state, today)} дн.")
    disabled = [
        entry.name
        for entry in sorted(PROFILES_DIR.iterdir())
        if entry.is_dir() and (entry / DISABLED_MARKER).exists()
    ]
    if disabled:
        print(f"\nотключены: {', '.join(disabled)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", help="прогнать только этот аккаунт")
    parser.add_argument("--list", action="store_true", help="показать аккаунты и их состояние")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="сделать всё, кроме финального запроса на забор награды",
    )
    parser.add_argument("--headed", action="store_true", help="показать окно браузера")
    parser.add_argument("-v", "--verbose", action="store_true", help="подробный лог")
    args = parser.parse_args()

    today = date.today().isoformat()

    if args.list:
        return print_accounts(today)

    setup_logging(args.verbose)

    # Держим дескриптор до конца процесса: закрытие сняло бы блокировку.
    lock = acquire_lock()
    if lock is None:
        log.info("другой запуск уже идёт, выхожу")
        return EXIT_OK

    if args.account:
        if not profile_dir(args.account).is_dir():
            log.error(
                "аккаунт %s не заведён, запусти %s %s",
                args.account,
                platform_bits.how_to_run("login.py"),
                args.account,
            )
            return EXIT_NEED_LOGIN
        accounts = [args.account]
    else:
        accounts = discover_accounts()

    if not accounts:
        log.error(
                "не заведено ни одного аккаунта, запусти %s <имя>",
                platform_bits.how_to_run("login.py"),
            )
        notify("Табель: нет аккаунтов", "Запусти login.py, чтобы войти в аккаунт.")
        return EXIT_NEED_LOGIN

    log.info("аккаунтов в прогоне: %d (%s)", len(accounts), ", ".join(accounts))

    from playwright.sync_api import sync_playwright

    outcomes: list[Outcome] = []
    with sync_playwright() as playwright:
        for index, account in enumerate(accounts):
            if index:
                time.sleep(ACCOUNT_PAUSE_SEC)
            outcomes.append(
                process_account(playwright, account, args.dry_run, not args.headed, today)
            )

    claimed = [item.account for item in outcomes if item.kind == KIND_CLAIMED]
    log.info(
        "итог: забрано у %d из %d (%s)",
        len(claimed),
        len(outcomes),
        ", ".join(claimed) if claimed else "ни у кого",
    )

    code, title, body = summarize(outcomes)
    if title:
        notify(title, f"{body}\nПодробности: {LOG_FILE}", "critical" if code else "normal")
    return code


if __name__ == "__main__":
    sys.exit(main())
