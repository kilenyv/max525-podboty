"""Разбор и формирование ссылок на подботы.

MAX передаёт payload из ссылки `https://max.ru/<bot>?start=<payload>` в
событии BotStarted.payload. Ограничение — 128 символов, поэтому payload
должен быть коротким.

Формат: `s_<slug>`. Префикс `s_` оставлен на будущее, чтобы можно было
добавить другие типы ссылок (`promo_...`, `srv_...`) без конфликтов.
"""

from __future__ import annotations

import re
from urllib.parse import quote

PAYLOAD_PREFIX = "s_"
MAX_PAYLOAD_LENGTH = 128

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


def make_payload(slug: str) -> str:
    """Собирает payload для ссылки подбота."""
    payload = f"{PAYLOAD_PREFIX}{slug}"
    if len(payload) > MAX_PAYLOAD_LENGTH:  # pragma: no cover - слаг короче 32
        raise ValueError(f"Слишком длинный payload: {payload}")
    return payload


def parse_payload(payload: str | None) -> str | None:
    """Достаёт слаг подбота из payload.

    Терпима к мусору: пользователь может открыть бота вручную и вписать
    что угодно после `start=`. Возвращает None, если это не наша ссылка.
    """
    if not payload:
        return None

    value = payload.strip().lower()
    if value.startswith(PAYLOAD_PREFIX):
        value = value[len(PAYLOAD_PREFIX) :]

    if not _SLUG_RE.match(value):
        return None
    return value


def build_deep_link(bot_username: str, slug: str) -> str:
    """Полная ссылка на подбот для рассылки клиентам."""
    username = bot_username.strip().lstrip("@")
    return f"https://max.ru/{username}?start={quote(make_payload(slug))}"
