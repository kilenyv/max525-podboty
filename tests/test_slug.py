"""Слаги: транслитерация, безопасность для URL, разрешение конфликтов."""

from __future__ import annotations

import re

import pytest

from bot.domain.slug import (
    MAX_SLUG_LENGTH,
    build_slug_candidates,
    random_suffix,
    slugify,
    transliterate,
)


class TestTransliterate:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Маникюр", "manikyur"),
            ("Стрижка Анны", "strizhka anny"),
            ("Юрист Ирина", "yurist irina"),
            ("Ёлка", "elka"),
            ("Цветы", "tsvety"),
            ("Щётка", "schetka"),
        ],
    )
    def test_cyrillic_becomes_latin(self, raw: str, expected: str) -> None:
        assert transliterate(raw) == expected

    def test_latin_passes_through(self) -> None:
        assert transliterate("Manicure 24") == "manicure 24"


class TestSlugify:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Маникюр", "manikyur"),
            ("  Стрижка   Анны  ", "strizhka-anny"),
            ("Маникюр/Педикюр", "manikyur-pedikyur"),
            ("Мастер @ iris!", "master-iris"),
            ("---Маникюр---", "manikyur"),
            ("КВАРТИРА", "kvartira"),
        ],
    )
    def test_produces_url_safe_slug(self, raw: str, expected: str) -> None:
        assert slugify(raw) == expected

    def test_result_is_ascii_lowercase_and_dashes(self) -> None:
        slug = slugify("Яна, массаж спины №5")
        assert re.fullmatch(r"[a-z0-9-]+", slug), slug

    def test_length_is_capped(self) -> None:
        slug = slugify("а" * 100)
        assert len(slug) <= MAX_SLUG_LENGTH
        assert not slug.endswith("-")

    def test_emoji_and_symbols_disappear(self) -> None:
        assert slugify("🔥🔥 Маникюр") == "manikyur"

    def test_empty_input_gives_empty_slug(self) -> None:
        # Ошибка не маскируется: вызывающий код обязан подставить запасной вариант.
        assert slugify("🔥🔥") == ""


class TestCandidates:
    def test_readable_candidate_comes_first(self) -> None:
        assert build_slug_candidates("Маникюр")[0] == "manikyur"

    def test_conflicts_get_numbered_suffixes(self) -> None:
        candidates = build_slug_candidates("Маникюр")
        assert candidates[:3] == ["manikyur", "manikyur-2", "manikyur-3"]

    def test_every_candidate_is_unique_and_safe(self) -> None:
        candidates = build_slug_candidates("Маникюр")
        assert len(set(candidates)) == len(candidates)
        for slug in candidates:
            assert re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug), slug
            assert len(slug) <= MAX_SLUG_LENGTH

    def test_long_title_candidates_respect_the_limit(self) -> None:
        base = "а" * 80
        for slug in build_slug_candidates(base):
            assert len(slug) <= MAX_SLUG_LENGTH, slug

    def test_empty_title_falls_back_to_booking(self) -> None:
        assert build_slug_candidates("🔥")[0] == "booking"

    def test_last_candidate_is_random(self) -> None:
        # Аффиксы кончились — включается случайный суффикс.
        candidates = build_slug_candidates("Маникюр")
        assert re.fullmatch(r"booking-[a-z0-9]{6}", candidates[-1]), candidates[-1]

    def test_random_suffix_alphabet(self) -> None:
        assert re.fullmatch(r"[a-z0-9]{4}", random_suffix(4))
        assert len(random_suffix(10)) == 10
