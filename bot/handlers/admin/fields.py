"""Редактор полей формы записи.

Набор полей у каждого подбота свой: мастер сам решает, что клиент должен
оставить перед записью и какие поля обязательны.
"""

from __future__ import annotations

from typing import Any

from bot.domain import callback as cb
from bot.domain.enums import DEFAULT_FIELDS, FIELD_LABELS, FieldType
from bot.domain.models import FormField, Subbot
from bot.handlers.base import AppContext
from bot.logging_config import get_logger
from bot.services import text
from bot.services.ui import Btn, Row

log = get_logger(__name__)

#: Коды кнопок раздела.
KEY = "fld"

#: Типы, которые мастер может включить в форму. Поля по умолчанию плюс
#: свободный многострочный текст: DEFAULT_FIELDS — это стартовый набор,
#: а не предел возможностей.
EDITABLE_TYPES: tuple[FieldType, ...] = (
    *(item[0] for item in DEFAULT_FIELDS),
    FieldType.TEXT,
)


def all_types() -> tuple[FieldType, ...]:
    """Поля, которые мастер может включать и выключать."""
    return EDITABLE_TYPES


async def handle_param(ctx: AppContext, event: Any, subbot: Subbot, parts: list[str]) -> bool:
    """Обрабатывает p:<id>:fld:<тип>[:off].

    Нажатие на включённое необязательное поле делает его обязательным,
    на обязательное — необязательным, на «выключено» — включает заново.

    Возвращает False, если код относится не к разделу полей: разбор кода
    общий для всей админки, и молча отглонив чужой код, мы проглотили бы
    его вместо того, чтобы отдать следующему разделу.
    """
    if len(parts) < 3 or parts[2] != KEY:
        return False

    if len(parts) < 4:
        return True

    field_type = _parse_field(parts[3])
    if field_type is None:
        # Код с неизвестным полем приходит только из подделанного payload.
        # Молчать нельзя: клиент нажал кнопку и не получил ответа.
        await show_fields(ctx, event, subbot)
        return True

    disable = len(parts) > 4 and parts[4] == "off"
    user_id = subbot.owner_user_id
    current = current_field(subbot, field_type)

    if current is None:
        # Включаем необязательным: лишний шаг не должен ломать запись.
        await ctx.repos.subbots.set_field_enabled(
            subbot.id, user_id, field_type, enabled=True, is_required=False
        )
    elif disable:
        await ctx.repos.subbots.set_field_enabled(subbot.id, user_id, field_type, enabled=False)
    else:
        await ctx.repos.subbots.set_field_required(subbot.id, user_id, field_type, not current.is_required)

    await show_fields(ctx, event, await _reload(ctx, subbot))
    return True


async def show_fields(ctx: AppContext, event: Any, subbot: Subbot) -> None:
    """Экран полей формы с переключателями включения и обязательности."""
    current = {item.field_type: item for item in subbot.form_fields}
    rows: list[Row] = []

    for field_type in all_types():
        item = current.get(field_type)
        if item is None:
            rows.append([Btn(f"➕ {FIELD_LABELS[field_type]}", payload=_code(subbot.id, field_type.value))])
        elif item.is_required:
            rows.append(
                [Btn(f"✅ {item.label} — обязательное", payload=_code(subbot.id, field_type.value, "off"))]
            )
        else:
            rows.append([Btn(f"◻️ {item.label} — необязательное", payload=_code(subbot.id, field_type.value))])

    rows.append([Btn(text.BUTTON_BACK, payload=cb.subbot_card(subbot.id))])

    header = text.fields_view(subbot)
    header += "\n\nНажмите на поле: ◻️ сделает обязательным, ✅ — необязательным, ➕ включит заново."
    await ctx.answer(event, header, rows)


def _code(subbot_id: int, field_type: str, *args: str) -> str:
    return cb.param_set(subbot_id, KEY, field_type, *args)


def _parse_field(value: str) -> FieldType | None:
    for field_type in all_types():
        if value == field_type.value:
            return field_type
    return None


def current_field(subbot: Subbot, field_type: FieldType) -> FormField | None:
    for item in subbot.form_fields:
        if item.field_type is field_type:
            return item
    return None


async def _reload(ctx: AppContext, subbot: Subbot) -> Subbot:
    refreshed = await ctx.repos.subbots.get_by_id(subbot.id)
    return refreshed or subbot


__all__ = ["EDITABLE_TYPES", "all_types", "current_field", "handle_param", "show_fields"]
