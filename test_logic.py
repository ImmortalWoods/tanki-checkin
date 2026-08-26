#!/usr/bin/env python3
"""Тесты чистой логики checkin.py. Без сети и без браузера."""

from __future__ import annotations

import pathlib
from datetime import datetime, timezone

import pytest

import platform_bits
from checkin import (
    EXIT_API_CHANGED,
    EXIT_NETWORK,
    EXIT_NEED_LOGIN,
    EXIT_OK,
    KIND_API_ERROR,
    KIND_CLAIMED,
    KIND_NETWORK_ERROR,
    KIND_NEED_LOGIN,
    KIND_NOOP,
    ApiError,
    NeedLogin,
    Outcome,
    amounts_from_entitlements,
    available_codes,
    days_without_claim,
    delivery_check,
    entitlement_names,
    is_already_claimed,
    is_not_logged_in,
    already_claimed_today,
    choose_code,
    day_of,
    month_token,
    preferred_code,
    parse_timestamp_ms,
    product_codes,
    purchase_body,
    require_ok,
    response_errors,
    stale_warning,
    summarize,
    valid_account_name,
)


def ms(*args) -> int:
    """Метка времени в миллисекундах из UTC-даты."""
    return int(datetime(*args, tzinfo=timezone.utc).timestamp() * 1000)


# --- токен месяца ---------------------------------------------------------


def test_month_token_matches_live_sample():
    # 1785752384 — реальный ответ /timestamp/, полученный 2026-08-03.
    assert month_token(1785752384 * 1000) == "aug26"


@pytest.mark.parametrize(
    "moment, expected",
    [
        ((2026, 1, 1, 0, 0), "jan26"),
        ((2026, 8, 3, 12, 0), "aug26"),
        ((2026, 12, 31, 23, 59), "dec26"),
        ((2027, 1, 1, 0, 0), "jan27"),  # граница года
        ((2100, 3, 15, 12, 0), "mar00"),  # граница века: slice(2) от "2100"
        ((2009, 5, 5, 0, 0), "may09"),  # год с ведущим нулём
    ],
)
def test_month_token_boundaries(moment, expected):
    assert month_token(ms(*moment)) == expected


def test_month_token_uses_utc_not_local():
    """Виджет считает по getUTCMonth: 1 сентября 00:30 UTC — уже sep, не aug."""
    assert month_token(ms(2026, 9, 1, 0, 30)) == "sep26"
    assert month_token(ms(2026, 8, 31, 23, 30)) == "aug26"


# --- имена кодов ----------------------------------------------------------


def test_entitlement_names():
    assert entitlement_names("aug26") == [
        "aug26ru_counter_claim",
        "daily_claim",
        "daily_complete",
    ]


def test_product_codes_cover_whole_month():
    codes = product_codes("aug26")
    assert len(codes) == 31
    assert codes[0] == "claim_aug26_1"
    assert codes[-1] == "claim_aug26_31"


# --- выбор дня ------------------------------------------------------------


def test_already_claimed_today_when_only_claim_flag_set():
    """Живое состояние twink сразу после забора 2026-08-03: ровно {'daily_claim': 1}."""
    assert already_claimed_today("aug26", {"daily_claim": 1}) is not None


def test_not_claimed_when_both_flags_set():
    """Обе метки выставлены — по логике виджета день не считается пройденным.

    Это состояние main на 2026-08-03: счётчик 2, daily_claim и daily_complete по 1.
    """
    assert already_claimed_today("aug26", {"aug26ru_counter_claim": 2, "daily_claim": 1, "daily_complete": 1}) is None


def test_not_claimed_on_empty_entitlements():
    """Состояние twink до первого забора: энтайтлментов нет вовсе."""
    assert already_claimed_today("aug26", {}) is None


def test_preferred_code_first_day_of_month():
    assert preferred_code("aug26", {}) == "claim_aug26_1"


def test_preferred_code_follows_counter():
    assert preferred_code("aug26", {"aug26ru_counter_claim": 7}) == "claim_aug26_8"


def test_preferred_code_none_after_last_day():
    assert preferred_code("aug26", {"aug26ru_counter_claim": 31}) is None


def test_preferred_code_ignores_other_months_counter():
    """Счётчик прошлого месяца не должен влиять на текущий."""
    assert preferred_code("sep26", {"aug26ru_counter_claim": 20}) == "claim_sep26_1"


@pytest.mark.parametrize(
    "code, expected", [("claim_aug26_1", 1), ("claim_aug26_31", 31), ("странное", 32)]
)
def test_day_of(code, expected):
    assert day_of(code) == expected


def test_choose_code_takes_counter_target_when_offered():
    code, _ = choose_code("aug26", {"aug26ru_counter_claim": 2}, {"claim_aug26_3"})
    assert code == "claim_aug26_3"


def test_choose_code_reproduces_twink_first_claim():
    """Живой случай: энтайтлментов нет, сервер предложил ровно claim_aug26_1."""
    code, _ = choose_code("aug26", {}, {"claim_aug26_1"})
    assert code == "claim_aug26_1"


def test_choose_code_trusts_server_over_stale_counter():
    """Счётчик не обновился и указывает на уже купленный день — берём то, что дают.

    Ровно этот случай похоронил бы аккаунт молча: арифметика вечно целилась бы
    в claim_aug26_1, которого в списке нет.
    """
    code, reason = choose_code("aug26", {}, {"claim_aug26_2"})
    assert code == "claim_aug26_2"
    assert "счётчик" in reason


def test_choose_code_picks_earliest_when_server_offers_several():
    code, _ = choose_code("aug26", {}, {"claim_aug26_10", "claim_aug26_2", "claim_aug26_31"})
    assert code == "claim_aug26_2"


def test_choose_code_none_on_empty_offer():
    """Живой случай main 2026-08-03: сервер вернул пустой список."""
    code, reason = choose_code("aug26", {"aug26ru_counter_claim": 2}, set())
    assert code is None
    assert "ни одного" in reason


def test_choose_code_falls_back_past_last_day():
    """Счётчик уехал за 31, но сервер что-то предлагает — берём предложенное."""
    code, _ = choose_code("aug26", {"aug26ru_counter_claim": 31}, {"claim_aug26_5"})
    assert code == "claim_aug26_5"


# --- разбор ответов -------------------------------------------------------


def test_parse_timestamp_ms():
    assert parse_timestamp_ms({"status": 200, "data": 1785752384}) == 1785752384000


def test_parse_timestamp_rejects_non_number():
    with pytest.raises(ApiError):
        parse_timestamp_ms({"status": 200, "data": None, "text": "<html>"})


def test_parse_timestamp_rejects_bool():
    """True прошёл бы проверку isinstance(..., int) — отсекаем явно."""
    with pytest.raises(ApiError):
        parse_timestamp_ms({"status": 200, "data": True})


def test_amounts_from_entitlements():
    payload = {
        "status": "ok",
        "data": [
            {"code": "aug26ru_counter_claim", "amount": 5},
            {"code": "daily_claim", "amount": 1},
            {"code": "daily_complete", "amount": 0},
        ],
    }
    assert amounts_from_entitlements(payload) == {
        "aug26ru_counter_claim": 5,
        "daily_claim": 1,
        "daily_complete": 0,
    }


def test_amounts_tolerates_junk_rows():
    payload = {"data": [{"code": "a", "amount": "3"}, "мусор", {"amount": 9}, {"code": "b"}]}
    assert amounts_from_entitlements(payload) == {"a": 3, "b": 0}


def test_amounts_rejects_non_list():
    with pytest.raises(ApiError):
        amounts_from_entitlements({"data": {"unexpected": "shape"}})


def test_available_codes():
    payload = {"data": {"items": [{"product_code": "claim_aug26_3"}, {"product_code": "x"}]}}
    assert available_codes(payload) == {"claim_aug26_3", "x"}


def test_available_codes_rejects_missing_items():
    with pytest.raises(ApiError):
        available_codes({"data": {}})


# --- признаки состояния ---------------------------------------------------


def test_is_not_logged_in_from_live_403():
    """Реальный ответ сервера без cookie, снятый 2026-08-03."""
    resp = {"status": 403, "data": {"status": "error", "errors": ["User is not logged in"]}}
    assert is_not_logged_in(resp)


def test_is_not_logged_in_from_message_alone():
    assert is_not_logged_in({"status": 200, "data": {"errors": ["User is not logged in"]}})


def test_is_not_logged_in_false_on_success():
    assert not is_not_logged_in({"status": 200, "data": {"status": "ok", "data": []}})


def test_response_errors_on_list_payload():
    """У энтайтлментов data — список; извлечение ошибок не должно падать."""
    assert response_errors({"status": 200, "data": [{"code": "a"}]}) == []


def test_is_already_claimed():
    assert is_already_claimed({"data": {"errors": ["Product already purchased"]}})
    assert not is_already_claimed({"data": {"errors": ["Insufficient funds"]}})


def test_require_ok_raises_need_login():
    with pytest.raises(NeedLogin):
        require_ok({"status": 403, "data": {"errors": ["User is not logged in"]}}, "тест")


def test_require_ok_raises_on_error_status():
    with pytest.raises(ApiError):
        require_ok({"status": 200, "data": {"status": "error", "errors": ["oops"]}}, "тест")


def test_require_ok_raises_on_html_response():
    """Заглушка вместо JSON — например, страница техработ."""
    with pytest.raises(ApiError):
        require_ok({"status": 200, "data": None, "text": "<html>maintenance</html>"}, "тест")


def test_require_ok_returns_payload():
    payload = {"status": "ok", "data": {"items": []}}
    assert require_ok({"status": 200, "data": payload}, "тест") is payload


# --- тело запроса на забор ------------------------------------------------


def test_purchase_body_matches_widget():
    body = purchase_body("claim_aug26_3")
    assert body["product_code"] == "claim_aug26_3"
    assert body["language"] == "ru"
    assert body["expected_prices"] == [{"code": "gold", "amount": "0", "item_type": "currency"}]
    assert len(body["transaction_id"]) == 36


def test_purchase_body_transaction_id_is_fresh_each_time():
    assert purchase_body("x")["transaction_id"] != purchase_body("x")["transaction_id"]


# --- доставка награды через заход в клиент ---------------------------------


def test_delivery_check_silent_without_pending():
    assert delivery_check({}, 0, "2026-08-04") == (None, False)


def test_delivery_check_silent_on_the_day_of_claim():
    """Забрали сегодня — дедлайн 3:00 МСК ещё не наступил, молчим."""
    pending = {"pending": {"date": "2026-08-04", "code": "claim_aug26_4", "counter_before": 3}}
    assert delivery_check(pending, 3, "2026-08-04") == (None, False)


def test_delivery_check_counter_moved_means_delivered():
    """Счётчик считает доставленные дни: сдвинулся — значит в клиент заходили."""
    pending = {"pending": {"date": "2026-08-03", "code": "claim_aug26_1", "counter_before": 0}}
    warning, forget = delivery_check(pending, 1, "2026-08-04")
    assert warning is None
    assert forget is True


def test_delivery_check_reports_lost_reward():
    """Ровно сценарий twink: забрали 3-го, в клиент не зашли, счётчик стоит."""
    pending = {"pending": {"date": "2026-08-03", "code": "claim_aug26_1", "counter_before": 0}}
    warning, forget = delivery_check(pending, 0, "2026-08-04")
    assert warning is not None
    assert "claim_aug26_1" in warning and "2026-08-03" in warning
    # Забываем сразу: ежедневно повторять одно и то же незачем, о затяжной
    # проблеме скажет страховка от простоя.
    assert forget is True


def test_delivery_check_survives_junk_pending():
    assert delivery_check({"pending": "мусор"}, 5, "2026-08-04") == (None, False)


# --- страховка от тихого простоя ------------------------------------------


def test_days_without_claim_counts_from_last_claim():
    assert days_without_claim({"last_claim": "2026-08-01", "since": "2026-07-01"}, "2026-08-05") == 4


def test_days_without_claim_counts_from_install_when_never_claimed():
    assert days_without_claim({"last_claim": None, "since": "2026-08-03"}, "2026-08-06") == 3


def test_days_without_claim_on_empty_state():
    """Первый запуск: точки отсчёта ещё нет, жаловаться не на что."""
    assert days_without_claim({}, "2026-08-03") == 0


def test_days_without_claim_survives_corrupt_state():
    assert days_without_claim({"last_claim": "не дата"}, "2026-08-03") == 0


def test_days_without_claim_never_negative():
    """Часы могли уехать назад — отрицательный простой бессмысленен."""
    assert days_without_claim({"last_claim": "2026-08-10"}, "2026-08-03") == 0


def test_stale_warning_silent_within_threshold():
    assert stale_warning({"last_claim": "2026-08-01"}, "2026-08-04") is None


def test_stale_warning_fires_past_threshold():
    warning = stale_warning({"last_claim": "2026-08-01"}, "2026-08-05")
    assert warning is not None
    assert "2026-08-01" in warning


def test_stale_warning_mentions_fresh_install():
    warning = stale_warning({"last_claim": None, "since": "2026-08-01"}, "2026-08-06")
    assert warning is not None
    assert "с момента установки" in warning


# --- имена аккаунтов ------------------------------------------------------


@pytest.mark.parametrize("name", ["main", "второй", "alt-2", "acc_1", "a.b", "X"])
def test_valid_account_names(name):
    assert valid_account_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "",  # пусто
        ".",  # текущий каталог
        "..",  # родительский каталог
        ".hidden",  # начинается с точки
        "-dash",  # начинается с дефиса
        "a/b",  # разделитель пути
        "a b",  # пробел
        "x" * 33,  # длиннее 32 символов
    ],
)
def test_invalid_account_names(name):
    assert not valid_account_name(name)


# --- сводка по аккаунтам --------------------------------------------------


def test_summarize_nothing_to_say_is_silent():
    assert summarize([Outcome("main", KIND_NOOP, "нечего")]) == (EXIT_OK, None, None)


def test_summarize_claim_reminds_about_client():
    """Забор без захода в клиент пропадает, поэтому успех тоже требует действия."""
    outcomes = [
        Outcome("main", KIND_CLAIMED, "забрано", code="claim_aug26_4"),
        Outcome("alt", KIND_NOOP, "нечего"),
    ]
    code, title, body = summarize(outcomes)
    assert code == EXIT_OK
    assert "клиент" in title
    assert "main (день 4)" in body
    assert "alt" not in body


def test_summarize_reports_broken_accounts_alongside_claims():
    outcomes = [
        Outcome("main", KIND_CLAIMED, "забрано", code="claim_aug26_4"),
        Outcome("alt", KIND_NEED_LOGIN, "сессия"),
    ]
    code, title, body = summarize(outcomes)
    # Заголовок по худшему исходу, но напоминание про клиент не теряется.
    assert code == EXIT_NEED_LOGIN
    assert "повторный вход" in title
    assert "alt" in body and "main (день 4)" in body


def test_summarize_reports_undelivered():
    outcomes = [Outcome("twink", KIND_NOOP, "нечего", undelivered="награда claim_aug26_1 …")]
    code, title, body = summarize(outcomes)
    assert code == EXIT_OK
    assert "не доставлена" in title
    assert "twink" in body


def test_summarize_api_error_outranks_need_login():
    """Смена API ломает всё и чинится только правкой скрипта — она важнее."""
    outcomes = [
        Outcome("a", KIND_NEED_LOGIN, "сессия"),
        Outcome("b", KIND_API_ERROR, "формат"),
        Outcome("c", KIND_NETWORK_ERROR, "сеть"),
    ]
    code, title, body = summarize(outcomes)
    assert code == EXIT_API_CHANGED
    assert "неожиданно" in title
    # В тексте всё равно перечислено всё, что сломалось.
    assert "a" in body and "b" in body and "c" in body


def test_summarize_need_login_outranks_network():
    outcomes = [Outcome("a", KIND_NETWORK_ERROR, "сеть"), Outcome("b", KIND_NEED_LOGIN, "сессия")]
    assert summarize(outcomes)[0] == EXIT_NEED_LOGIN


def test_summarize_network_alone():
    code, title, body = summarize([Outcome("a", KIND_NETWORK_ERROR, "сеть")])
    assert code == EXIT_NETWORK
    assert "достучаться" in body


def test_summarize_stale_warns_without_failing():
    """Простой — повод присмотреться, но не отказ: код возврата остаётся нулевым."""
    outcomes = [Outcome("main", KIND_NOOP, "нечего", stale="5 дней без наград: …")]
    code, title, body = summarize(outcomes)
    assert code == EXIT_OK
    assert title is not None
    assert "давно нет наград: main" in body


def test_summarize_groups_several_accounts_of_one_kind():
    outcomes = [
        Outcome("a", KIND_NEED_LOGIN, "сессия"),
        Outcome("b", KIND_NEED_LOGIN, "сессия"),
    ]
    assert "a, b" in summarize(outcomes)[2]


# --- различия платформ ------------------------------------------------------
#
# Windows-ветки проверяются подменой флага: живой Windows под рукой нет, но
# опечатку в пути или в выборе команды такие тесты ловят.


@pytest.fixture
def on_windows(monkeypatch):
    monkeypatch.setattr(platform_bits, "IS_WINDOWS", True)
    monkeypatch.delenv("TANKI_CHECKIN_DATA", raising=False)
    monkeypatch.delenv("TANKI_CHECKIN_STATE", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\tester\AppData\Local")


@pytest.fixture
def on_linux(monkeypatch):
    monkeypatch.setattr(platform_bits, "IS_WINDOWS", False)
    monkeypatch.delenv("TANKI_CHECKIN_DATA", raising=False)
    monkeypatch.delenv("TANKI_CHECKIN_STATE", raising=False)


# Сравниваем пути объектами, а не строками: на Linux PosixPath не считает "\\"
# разделителем, и проверка по хвосту строки врала бы.
def test_windows_data_dir_lives_in_local_appdata(on_windows):
    import os

    expected = pathlib.Path(os.environ["LOCALAPPDATA"]) / "tanki-checkin"
    assert platform_bits.data_dir() == expected


def test_windows_state_dir_sits_next_to_data(on_windows):
    """На Windows нет отдельного места под состояние — кладём подкаталогом."""
    assert platform_bits.state_dir() == platform_bits.data_dir() / "state"


def test_windows_falls_back_when_localappdata_missing(on_windows, monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    expected = pathlib.Path.home() / "AppData" / "Local" / "tanki-checkin"
    assert platform_bits.data_dir() == expected


def test_linux_keeps_xdg_layout(on_linux):
    assert platform_bits.data_dir().as_posix().endswith(".local/share/tanki-checkin")
    assert platform_bits.state_dir().as_posix().endswith(".local/state/tanki-checkin")


@pytest.mark.parametrize("where", ["on_windows", "on_linux"])
def test_env_override_wins_on_both(where, request, monkeypatch, tmp_path):
    request.getfixturevalue(where)
    monkeypatch.setenv("TANKI_CHECKIN_DATA", str(tmp_path / "d"))
    monkeypatch.setenv("TANKI_CHECKIN_STATE", str(tmp_path / "s"))
    assert platform_bits.data_dir() == tmp_path / "d"
    assert platform_bits.state_dir() == tmp_path / "s"


def test_user_agent_matches_the_system(on_windows):
    assert "Windows NT" in platform_bits.user_agent()


def test_user_agent_linux(on_linux):
    assert "X11; Linux" in platform_bits.user_agent()


def _capture_run(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))

    monkeypatch.setattr(platform_bits.subprocess, "run", fake_run)
    return calls


def test_windows_notification_goes_through_powershell(on_windows, monkeypatch):
    calls = _capture_run(monkeypatch)
    platform_bits.notify("Заголовок", "Тело")
    command, kwargs = calls[0]
    assert command[0] == "powershell"
    # Текст передаётся окружением, а не подстановкой в скрипт: кавычка в
    # заголовке иначе сломала бы команду.
    assert kwargs["env"]["TANKI_TOAST_TITLE"] == "Заголовок"
    assert kwargs["env"]["TANKI_TOAST_BODY"] == "Тело"


def test_windows_notification_survives_quotes(on_windows, monkeypatch):
    calls = _capture_run(monkeypatch)
    platform_bits.notify('Кавычка " и \'апостроф\'', "Тело")
    assert 'Кавычка " и \'апостроф\'' == calls[0][1]["env"]["TANKI_TOAST_TITLE"]


def test_linux_notification_goes_through_notify_send(on_linux, monkeypatch):
    calls = _capture_run(monkeypatch)
    platform_bits.notify("Заголовок", "Тело", "normal")
    command, _ = calls[0]
    assert command[0] == "notify-send"
    assert "Заголовок" in command and "Тело" in command


def test_missing_notification_tool_does_not_break_the_run(on_linux, monkeypatch):
    """Уведомление — не повод ронять забор."""

    def explode(*args, **kwargs):
        raise FileNotFoundError("notify-send")

    monkeypatch.setattr(platform_bits.subprocess, "run", explode)
    platform_bits.notify("Заголовок", "Тело")


def test_lock_is_exclusive(tmp_path):
    """Второй прогон не должен получить замок, который держит первый."""
    import os

    path = tmp_path / "lock"
    first = os.open(path, os.O_CREAT | os.O_RDWR)
    second = os.open(path, os.O_CREAT | os.O_RDWR)
    try:
        assert platform_bits.lock_exclusive(first)
        assert not platform_bits.lock_exclusive(second)
    finally:
        os.close(first)
        os.close(second)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
