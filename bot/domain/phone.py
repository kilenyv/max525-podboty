"""Валидация и нормализация телефонных номеров.

Рассчитано на номера РФ, но не отказывает международным форматам: клиент
может оставить номер в любой стране, главное — чтобы он был похож на телефон.
"""

from __future__ import annotations

import re

_DIGITS = re.compile(r"\D")
_FORMATTED = re.compile(r"[\s\-().]")

#: Допустимое число значащих цифр в номере. E.164 разрешает до 15.
_MIN_DIGITS = 7
_MAX_DIGITS = 15


def _digits(value: str) -> str:
    return _DIGITS.sub("", value)


def normalize_phone(raw: str) -> str | None:
    """Приводит ввод к каноническому виду `+7 (999) 123-45-67`.

    Возвращает None, если номер не похож на телефон. Номера не из России
    остаются в E.164 (`+<цифры>`): угадать границу кода страны в
    произвольном наборе цифр нельзя, а выдумывать группировку не стоит.
    """
    if not raw:
        return None

    cleaned = _FORMATTED.sub("", raw.strip())

    # Повторяющийся плюс или мусор вроде «+7 (999) abc» — отклоняем.
    if cleaned.count("+") > 1 or any(char.isalpha() for char in cleaned):
        return None

    digits = _digits(cleaned)
    if not (_MIN_DIGITS <= len(digits) <= _MAX_DIGITS):
        return None

    national = digits
    if digits.startswith("8") and len(digits) == 11:
        national = "7" + digits[1:]
    elif digits.startswith("00"):
        national = digits[2:]
    elif len(digits) == 10 and digits.startswith("9"):
        # Мобильный в РФ без кода страны — «999 123 45 67» почти всегда
        # значит +7. Ошибиться тут можно только с номером другой страны,
        # начинающимся на девятку, — но пишут его тоже с плюсом.
        national = "7" + digits

    national = national.lstrip("0") or national

    if national.startswith("7") and len(national) == 11:
        return f"+7 ({national[1:4]}) {national[4:7]}-{national[7:9]}-{national[9:11]}"
    return f"+{national}"


def is_valid_phone(raw: str) -> bool:
    return normalize_phone(raw) is not None
