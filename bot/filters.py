"""Фильтры для нажатий на inline-кнопки.

maxapi принимает в качестве фильтра только свои ``BaseFilter``,
``MagicFilter`` или ``State``. Обычная лямбда туда не подходит: она
молча игнорируется, обработчик начинает ловить всё подряд, а срабатывает
первый зарегистрированный. Поэтому фильтры кнопок — обычные классы.

Коды кнопок короткие, поэтому одного префикса достаточно, чтобы развести
все разделы: `a:` — админка, `d:`/`t:` — клиент, `c:`/`y:` — отмена.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from maxapi.filters.filter import BaseFilter

from bot.domain import callback as cb


def _payload_of(event: Any) -> str:
    callback = getattr(event, "callback", None)
    if callback is None:
        return ""
    return str(getattr(callback, "payload", None) or "")


class PayloadIs(BaseFilter):
    """Срабатывает на точный код кнопки."""

    def __init__(self, *codes: str) -> None:
        self.codes = frozenset(codes)

    async def __call__(self, event: Any) -> bool:
        return _payload_of(event) in self.codes

    def __repr__(self) -> str:
        return f"PayloadIs({sorted(self.codes)})"


class PayloadPrefix(BaseFilter):
    """Срабатывает, если код кнопки начинается с указанного префикса."""

    def __init__(self, *prefixes: str) -> None:
        self.prefixes = tuple(prefixes)

    async def __call__(self, event: Any) -> bool:
        payload = _payload_of(event)
        return bool(payload) and payload.startswith(self.prefixes)

    def __repr__(self) -> str:
        return f"PayloadPrefix{self.prefixes}"


class CallbackAnd(BaseFilter):
    """Комбинация фильтров: срабатывает, только если сработали все."""

    def __init__(self, *filters: BaseFilter) -> None:
        self.filters = filters

    async def __call__(self, event: Any) -> bool:
        for item in self.filters:
            if not await item(event):
                return False
        return True

    def __repr__(self) -> str:
        return f"CallbackAnd({self.filters!r})"


class CallbackAny(BaseFilter):
    """Комбинация фильтров: срабатывает, если сработал хотя бы один."""

    def __init__(self, *filters: BaseFilter) -> None:
        self.filters = filters

    async def __call__(self, event: Any) -> bool:
        for item in self.filters:
            if await item(event):
                return True
        return False

    def __repr__(self) -> str:
        return f"CallbackAny({self.filters!r})"


def is_code(*codes: str) -> PayloadIs:
    return PayloadIs(*codes)


def starts_with(*prefixes: str) -> PayloadPrefix:
    return PayloadPrefix(*prefixes)


def match(predicate: Callable[[str], bool], description: str = "custom") -> BaseFilter:
    """Фильтр по произвольному правилу на код кнопки.

    Для редких случаев, когда правило не сводится к префиксу.
    """

    class _Predicate(BaseFilter):
        async def __call__(self, event: Any) -> bool:
            payload = _payload_of(event)
            return bool(payload) and predicate(payload)

        def __repr__(self) -> str:
            return f"match({description})"

    return _Predicate()


def admin_buttons() -> list[BaseFilter]:
    """Пресеты префиксов админки — чтобы не расходились по буквам."""
    return [
        PayloadPrefix("a:"),
        PayloadPrefix("h:"),
        PayloadPrefix("p:"),
        PayloadPrefix("i:"),
        PayloadPrefix("r:"),
        PayloadPrefix("e:"),
        PayloadPrefix("q:"),
    ]


#: Коды, которые обрабатывает корневое меню.
MENU_CODES = (cb.MENU_HOME, cb.MENU_BOOK, cb.MENU_MINE, cb.MENU_PANEL, cb.MENU_HELP)


__all__ = [
    "MENU_CODES",
    "CallbackAnd",
    "CallbackAny",
    "PayloadIs",
    "PayloadPrefix",
    "admin_buttons",
    "is_code",
    "match",
    "starts_with",
]
