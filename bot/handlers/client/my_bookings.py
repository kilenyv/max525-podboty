"""Свои записи и отмена.

Логика отмены одна и та же для обеих сторон: клиент отменяет запись себе,
владелец — клиенту. Различаются только лимит по времени и то, кому
уходит уведомление.
"""

from __future__ import annotations

import time

from maxapi.types import MessageCallback

from bot.domain import callback as cb
from bot.domain.enums import CancelSource
from bot.domain.models import Booking, Subbot
from bot.filters import is_code, starts_with
from bot.handlers.base import AppContext, callback_payload, user_id_of
from bot.logging_config import get_logger
from bot.services import text
from bot.services.ui import Btn, Row

log = get_logger(__name__)


async def register_my_bookings(dp, ctx: AppContext) -> None:  # noqa: ANN001
    """Регистрирует обработчики списка записей и отмены."""

    @dp.message_callback(is_code(cb.MENU_MINE))
    async def on_my_bookings(event: MessageCallback) -> None:
        await ctx.show_my_bookings(event)

    @dp.message_callback(starts_with("c:"))
    async def on_cancel_request(event: MessageCallback) -> None:
        """Первый шаг: показываем, что именно отменяем, и спрашиваем."""
        booking, subbot, source = await _resolve(ctx, event)
        if booking is None or subbot is None:
            return

        if not booking.is_active:
            await ctx.answer(event, "Эта запись уже отменена.", _mine_rows())
            return

        if source is CancelSource.CLIENT and not _client_may_cancel(subbot, booking):
            await ctx.answer(event, text.cancel_too_late(subbot.cancel_notice_hours), _mine_rows())
            return

        rows: list[Row] = [
            [Btn("Да, отменить", payload=cb.booking_cancel_confirm(booking.id))],
            [Btn("Оставить запись", payload=cb.MENU_MINE)],
        ]
        await ctx.answer(event, text.cancel_confirm(subbot.title, booking), rows)

    @dp.message_callback(starts_with("y:"))
    async def on_cancel_confirm(event: MessageCallback) -> None:
        """Второй шаг: собственно отмена."""
        booking, subbot, source = await _resolve(ctx, event)
        if booking is None or subbot is None:
            return

        if not await ctx.cancel_booking(subbot, booking, source):
            await ctx.answer(event, "Не получилось отменить запись.", _mine_rows())
            return

        done = (
            text.booking_cancelled_client if source is CancelSource.CLIENT else text.booking_cancelled_owner
        )
        body = done(subbot.title, booking, text.format_values(subbot, booking.values))
        await ctx.answer(event, body, _mine_rows())

    @dp.message_callback(starts_with("z:"))
    async def on_busy_slot(event: MessageCallback) -> None:
        """Клиент нажал на занятый слот."""
        await ctx.answer(event, text.slot_taken(), [])


# --- Вспомогательное --------------------------------------------------------


async def _resolve(
    ctx: AppContext, event: MessageCallback
) -> tuple[Booking | None, Subbot | None, CancelSource]:
    """Определяет, чья это запись: клиента или владельца подбота.

    Чужие записи по чужому коду кнопки недоступны: здесь возвращается None.
    """
    user_id = user_id_of(event)
    parts = cb.parse_callback(callback_payload(event))
    booking_id = cb.to_int(parts[1]) if len(parts) > 1 else None
    if user_id is None or booking_id is None:
        return None, None, CancelSource.CLIENT

    booking = await ctx.repos.bookings.get_by_id(booking_id)
    if booking is None:
        await ctx.answer(event, "Запись не найдена.", _mine_rows())
        return None, None, CancelSource.CLIENT

    subbot = await ctx.repos.subbots.get_by_id(booking.subbot_id)
    if subbot is None:
        await ctx.answer(event, "Запись не найдена.", _mine_rows())
        return None, None, CancelSource.CLIENT

    if booking.user_id == user_id:
        return booking, subbot, CancelSource.CLIENT

    if subbot.owner_user_id == user_id:
        return booking, subbot, CancelSource.OWNER

    log.warning("Пользователь %s попытался отменить чужую запись %s", user_id, booking_id)
    await ctx.answer(event, "Это не ваша запись.", [])
    return None, None, CancelSource.CLIENT


def _client_may_cancel(subbot: Subbot, booking: Booking) -> bool:
    """Лимит «не позднее чем за N часов» действует только для клиента."""
    return booking.starts_at - int(time.time()) >= subbot.cancel_notice_hours * 3600


def _mine_rows() -> list[Row]:
    return [[Btn(text.BUTTON_MINE, payload=cb.MENU_MINE)]]


__all__ = ["register_my_bookings"]
