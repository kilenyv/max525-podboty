"""Генерация слагов подботов.

Слаг попадает в публичную ссылку, поэтому должен быть коротким, читаемым
и безопасным для URL: только латиница в нижнем регистре, цифры и дефис.
"""

from __future__ import annotations

import random
import string

#: Раскладка кириллицы -> латиница для транслитерации.
_TRANSLIT: dict[str, str] = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "sch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}

MAX_SLUG_LENGTH = 32
_FALLBACK = "booking"
_SUFFIX_ALPHABET = string.ascii_lowercase + string.digits


def transliterate(value: str) -> str:
    result: list[str] = []
    for char in value.lower():
        result.append(_TRANSLIT.get(char, char))
    return "".join(result)


def slugify(value: str) -> str:
    """Приводит произвольное название к безопасному слагу.

    Пустая строка на выходе возможна — вызывающий код обязан проверить
    результат и подставить запасной вариант.
    """
    transliterated = transliterate(value)
    cleaned = "".join(
        char if char.isascii() and (char.isalnum() or char == "-") else "-" for char in transliterated
    )
    slug = "-".join(part for part in cleaned.split("-") if part)
    slug = slug.strip("-")
    return slug[:MAX_SLUG_LENGTH].strip("-")


def random_suffix(length: int = 4) -> str:
    return "".join(random.choices(_SUFFIX_ALPHABET, k=length))


def build_slug_candidates(title: str) -> list[str]:
    """Кандидаты слага: сначала читаемый, затем с цифровым суффиксом.

    Список конечен — репозиторий перебирает его и берёт первый свободный.
    """
    base = slugify(title) or _FALLBACK
    candidates = [base]
    for index in range(2, 100):
        suffix = f"-{index}"
        trimmed = base[: MAX_SLUG_LENGTH - len(suffix)].rstrip("-")
        candidates.append(f"{trimmed or _FALLBACK}{suffix}")
    candidates.append(f"{_FALLBACK}-{random_suffix(6)}")
    return candidates
