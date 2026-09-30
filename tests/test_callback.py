"""Коды кнопок: короткие, однозначно разбираемые, уникальные по префиксу."""

from __future__ import annotations

import pytest

from bot.domain import callback as cb

#: Все функции, которые собирают код кнопки.
BUILDERS = {
    "subbot_open": lambda: cb.subbot_open(123),
    "day_pick": lambda: cb.day_pick(123, 7),
    "slot_pick": lambda: cb.slot_pick(123, 7, 630),
    "slot_busy": lambda: cb.slot_busy(1789000000),
    "booking_cancel": lambda: cb.booking_cancel(456),
    "booking_cancel_confirm": lambda: cb.booking_cancel_confirm(456),
    "subbot_card": lambda: cb.subbot_card(123),
    "subbot_section": lambda: cb.subbot_section(123, cb.SECTION_SCHEDULE),
    "weekday_pick": lambda: cb.weekday_pick(123, 3),
    "profile_edit": lambda: cb.profile_edit(123, "title"),
    "subbot_delete": lambda: cb.subbot_delete(123),
    "subbot_delete_confirm": lambda: cb.subbot_delete_confirm(123),
    "param_set": lambda: cb.param_set(123, "rul", "horizon", "14"),
    "interval_delete": lambda: cb.interval_delete(123, 3, 2),
    "break_delete": lambda: cb.break_delete(123, 3, 0),
    "subbot_create": cb.subbot_create,
    "field_form_finish": cb.field_form_finish,
}


class TestLength:
    @pytest.mark.parametrize(("name", "builder"), sorted(BUILDERS.items()))
    def test_code_fits_the_limit(self, name: str, builder) -> None:
        code = builder()
        assert len(code) <= cb.MAX_CALLBACK_LENGTH, f"{name}: {code} ({len(code)})"

    def test_limit_is_stricter_than_the_api_allows(self) -> None:
        # MAX разрешает 256, но короткие коды переживают любые будущие
        # ограничения и не светятся в логах.
        assert cb.MAX_CALLBACK_LENGTH <= 128

    def test_absurdly_long_code_is_rejected(self) -> None:
        with pytest.raises(cb.CallbackError):
            cb.slot_pick(1, 2, 10**40)

    def test_slot_time_has_no_colon_inside(self) -> None:
        # «10:00» в коде распалось бы на «10» и «00»: разбор принял бы
        # первую часть за всё время. Поэтому в коде — минуты.
        parts = cb.parse_callback(cb.slot_pick(1, 0, 600))
        assert parts == ["t", "1", "0", "600"]


class TestGrammar:
    def test_parse_splits_on_colon(self) -> None:
        assert cb.parse_callback("t:12:3:10:30") == ["t", "12", "3", "10", "30"]

    def test_parse_of_empty_payload(self) -> None:
        assert cb.parse_callback("") == [""]
        assert cb.parse_callback(None) == [""]

    def test_booking_codes_use_different_letters(self) -> None:
        # Запрос отмены и подтверждение не должны совпадать: иначе одна
        # кнопка отменяла бы запись без вопроса.
        assert cb.booking_cancel(5) != cb.booking_cancel_confirm(5)

    def test_weekday_is_not_encoded_as_a_section(self) -> None:
        # `a:` — префикс разделов карточки; день недели живёт под `h:`.
        assert cb.weekday_pick(1, 3).startswith("h:")
        assert not hasattr(cb, "subbot_day")

    def test_param_without_value_is_three_parts(self) -> None:
        assert cb.parse_callback(cb.param_set(1, "tz_open")) == ["p", "1", "tz_open"]

    def test_param_with_two_args_keeps_them_in_order(self) -> None:
        # Аргументы не содержат двоеточий и остаются отдельными частями.
        assert cb.parse_callback(cb.param_set(1, "rul", "horizon", "14")) == [
            "p",
            "1",
            "rul",
            "horizon",
            "14",
        ]

    def test_to_int_is_forgiving(self) -> None:
        assert cb.to_int("12") == 12
        assert cb.to_int("-3") == -3
        assert cb.to_int("abc") is None
        assert cb.to_int(None) is None
        assert cb.to_int("") is None


class TestNoPrefixCollisions:
    """Каждая буква префикса принадлежит ровно одному обработчику."""

    #: Префиксы, на которые подписан каждый раздел админки и клиента.
    SECTION_PREFIXES = ("a:", "h:", "p:", "i:", "r:", "e:", "q:", "s:", "d:", "t:", "c:", "y:", "z:")

    def test_static_codes_have_their_own_prefixes(self) -> None:
        singles = {
            cb.MENU_HOME: "m:",
            cb.MENU_BOOK: "m:",
            cb.MENU_MINE: "m:",
            cb.MENU_PANEL: "m:",
            cb.MENU_HELP: "m:",
            cb.BOOKING_CONFIRM: "b:",
            cb.field_form_finish(): "f:",
            cb.subbot_create(): "n:",
        }
        for code, prefix in singles.items():
            assert code.startswith(prefix), code

    def test_root_menu_codes_do_not_collide(self) -> None:
        codes = {cb.MENU_HOME, cb.MENU_BOOK, cb.MENU_MINE, cb.MENU_PANEL, cb.MENU_HELP}
        assert len(codes) == 5

    def test_sections_have_distinct_names(self) -> None:
        sections = [
            cb.SECTION_SCHEDULE,
            cb.SECTION_FIELDS,
            cb.SECTION_RULES,
            cb.SECTION_BOOKINGS,
            cb.SECTION_LINK,
        ]
        assert len(set(sections)) == len(sections)
