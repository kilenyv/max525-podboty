"""Диплинк: ссылка должна дождаться клиента и пережить опечатки."""

from __future__ import annotations

import pytest

from bot.domain.deeplink import (
    MAX_PAYLOAD_LENGTH,
    build_deep_link,
    make_payload,
    parse_payload,
)
from bot.domain.slug import build_slug_candidates, slugify


class TestMakePayload:
    def test_prefix_marks_the_link_type(self) -> None:
        assert make_payload("manikur") == "s_manikur"

    def test_payload_fits_the_api_limit(self) -> None:
        for slug in build_slug_candidates("а" * 200)[:5]:
            assert len(make_payload(slug)) <= MAX_PAYLOAD_LENGTH

    def test_impossibly_long_payload_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            make_payload("x" * MAX_PAYLOAD_LENGTH)


class TestParsePayload:
    def test_round_trip(self) -> None:
        assert parse_payload(make_payload("strizhka-anny")) == "strizhka-anny"

    def test_payload_without_prefix_still_works(self) -> None:
        # MAX может отдать start= без префикса, если ссылку правили руками.
        assert parse_payload("manikur") == "manikur"

    def test_case_and_spaces_are_forgiven(self) -> None:
        assert parse_payload("  S_Manikur  ") == "manikur"

    @pytest.mark.parametrize(
        "raw",
        [None, "", "   ", "s_", "промо", "s_ПРОМО", "-manikur", "manikur!", "a" * 40],
    )
    def test_foreign_payloads_are_ignored(self, raw: str | None) -> None:
        # Чужой или испорченный payload не должен открывать чужой подбот.
        assert parse_payload(raw) is None

    def test_hyphen_inside_slug_is_allowed(self) -> None:
        assert parse_payload("s_strizhka-anny") == "strizhka-anny"


class TestBuildDeepLink:
    def test_link_shape(self) -> None:
        assert build_deep_link("mybot", "manikur") == "https://max.ru/mybot?start=s_manikur"

    def test_at_sign_is_stripped(self) -> None:
        assert build_deep_link("@mybot", "manikur") == "https://max.ru/mybot?start=s_manikur"

    def test_link_of_a_realistic_slug(self) -> None:
        link = build_deep_link("zapis_bot", slugify("Стрижка Анны"))
        assert link == "https://max.ru/zapis_bot?start=s_strizhka-anny"

    def test_payload_survives_a_round_trip_through_the_link(self) -> None:
        link = build_deep_link("bot", "manikur-2")
        payload = link.split("start=", 1)[1]
        assert parse_payload(payload) == "manikur-2"
