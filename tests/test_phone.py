"""Телефон: российский формат обязателен, мусор отвергается."""

from __future__ import annotations

import pytest

from bot.domain.phone import is_valid_phone, normalize_phone


class TestRussianNumbers:
    @pytest.mark.parametrize(
        "raw",
        [
            "+7 999 123-45-67",
            "+79991234567",
            "8 999 123 45 67",
            "89991234567",
            "7 (999) 123-45-67",
            "  +7-999-123-45-67  ",
            "+7(999)1234567",
        ],
    )
    def test_accepts_russian_formats(self, raw: str) -> None:
        assert normalize_phone(raw) == "+7 (999) 123-45-67"

    def test_leading_eight_becomes_seven(self) -> None:
        assert normalize_phone("8 999 123 45 67") == normalize_phone("+7 999 123 45 67")

    def test_mobile_without_country_code_gets_seven_prepended(self) -> None:
        # «999 123 45 67» без кода страны — почти всегда +7.
        assert normalize_phone("999 123 45 67") == "+7 (999) 123-45-67"


class TestRejection:
    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "не телефон",
            "+7 abc 123 45 67",
            "+7 999",
            "12345",  # слишком коротко
            "+1234567890123456",  # больше 15 цифр
            "+7+7 999 123 45 67",
        ],
    )
    def test_rejects_non_phone_input(self, raw: str) -> None:
        assert normalize_phone(raw) is None
        assert is_valid_phone(raw) is False

    def test_letters_hidden_among_digits_are_caught(self) -> None:
        assert normalize_phone("+7 999 123-45-6a") is None


class TestInternationalNumbers:
    def test_uzbek_number_stays_in_e164(self) -> None:
        # Код страны не угадать — оставляем цифры как есть, лишь с плюсом.
        assert normalize_phone("+998 90 123 45 67") == "+998901234567"

    def test_seven_digit_number_stays_short(self) -> None:
        assert normalize_phone("+371 2000") == "+3712000"

    def test_kazakh_number_gets_the_russian_grouping(self) -> None:
        # +7 701 ... — это тоже одиннадцать цифр, группировка та же.
        assert normalize_phone("+7 701 234 56 78") == "+7 (701) 234-56-78"

    def test_any_seven_to_fifteen_digits_is_enough(self) -> None:
        # Клиент может быть не из России: главное, что это похоже на телефон.
        assert is_valid_phone("+371 2000 0000") is True
        assert is_valid_phone("+1 202 555 0143") is True

    def test_00_international_prefix_is_understood(self) -> None:
        assert normalize_phone("0037 12 345 67 89") == "+37123456789"
