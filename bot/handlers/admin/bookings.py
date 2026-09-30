"""Записи подбота в админке.

Владелец видит только свои подботы — проверка владения выполняется
до чтения, поэтому чужие записи недоступны по коду кнопки.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from bot.domain import callback as cb
from bot.domain.models import Subbot
from bot.handlers.base import AppContext
from bot.logging_config import get_logger
from bot.services import text
from bot.services.ui import Btn, Row

log = get_logger(__name__)

#: Сколько записей показываем за раз — больше список нечитаем.
PREVIEW_LIMIT = 60


async def show_subbot_bookings(ctx: AppContext, event: Any, subbot: Subbot) -> None:
    """Список предстоящих записей подбота с кнопкой отмены у каждой."""
    bookings = await ctx.repos.bookings.list_subbot_upcoming(subbot.id, limit=PREVIEW_LIMIT)

    rows: list[Row] = []
    if not bookings:
        body = f"📋 <b>Записи: {subbot.title}</b>\n\nПредстоящих записей нет."
    else:
        lines = [f"📋 <b>Записи: {subbot.title}</b>", ""]
        for booking in bookings:
            values = text.format_values(subbot, booking.values).replace("\n", " · ") or "без данных"
            lines.append(
                f"• {booking.start_local:%d.%m} "
                f"{booking.start_local:%H:%M}–{booking.end_local:%H:%M} — {values}"
            )
            rows.append([Btn(f"🚫 {booking.start_local:%d.%m %H:%M}", payload=cb.booking_cancel(booking.id))])

        last_day = datetime.fromtimestamp(bookings[-1].ends_at, tz=bookings[-1].tz).date()
        lines.append("")
        lines.append(f"Всего: {len(bookings)} · последняя {last_day:%d.%m.%Y}")
        body = "\n".join(lines)

    rows.append([Btn(text.BUTTON_BACK, payload=cb.subbot_card(subbot.id))])
    await ctx.answer(event, body, rows)


__all__ = ["PREVIEW_LIMIT", "show_subbot_bookings"]
