"""Уведомления клиенту и владельцу.

Все сообщения, которые бот шлёт сам, собираются здесь — обработчики
работают с интерфейсом, но не с текстами оповещений.
"""

from __future__ import annotations

import time
from datetime import timedelta

from bot.db.connection import Database
from bot.db.repositories import Repositories
from bot.domain import callback as cb
from bot.domain.enums import CancelSource
from bot.domain.models import Booking, Subbot
from bot.domain.timeutil import localize, to_unix
from bot.logging_config import get_logger
from bot.services import text
from bot.services.booking import BookingService, local_date
from bot.services.max_sender import MaxSender
from bot.services.ui import Btn, Row

log = get_logger(__name__)


class Notifier:
    """Уведомления клиенту и владельцу.

    Обработчики работают с интерфейсом, но не с текстами оповещений:
    всё, что бот шлёт сам, собирается здесь.
    """

    def __init__(
        self,
        db: Database,
        repos: Repositories,
        sender: MaxSender,
        booking_service: BookingService,
    ) -> None:
        self.db = db
        self.repos = repos
        self.sender = sender
        self.booking = booking_service

    # --- Новая запись ------------------------------------------------------

    async def notify_owner_of_booking(self, subbot: Subbot, booking: Booking) -> None:
        """Мгновенное уведомление владельцу с кнопками управления."""
        body = text.booking_card(subbot.title, booking, text.format_values(subbot, booking.values))
        rows: list[Row] = [
            [Btn("Отменить запись", payload=cb.booking_cancel(booking.id))],
            [Btn("К подботу", payload=cb.subbot_open(subbot.id))],
        ]
        await self.sender.send(subbot.owner_user_id, body, rows)

    async def notify_client_confirmed(self, subbot: Subbot, booking: Booking) -> None:
        body = text.booking_created(subbot, booking, text.format_values(subbot, booking.values))
        rows: list[Row] = [[Btn(text.BUTTON_MINE, payload=cb.MENU_MINE)]]
        await self.sender.send(booking.user_id, body, rows)

    # --- Отмена ------------------------------------------------------------

    async def notify_owner_of_cancel(self, subbot: Subbot, booking: Booking, source: CancelSource) -> None:
        who = "Клиент отменил запись" if source is CancelSource.CLIENT else "Вы отменили запись"
        body = f"🚫 <b>{who}</b>\n\n" + text.booking_cancelled_owner(
            subbot.title, booking, text.format_values(subbot, booking.values)
        )
        await self.sender.send(subbot.owner_user_id, body, [])

    async def notify_client_of_cancel(self, subbot: Subbot, booking: Booking) -> None:
        body = text.booking_cancelled_client(subbot.title, booking)
        rows: list[Row] = [[Btn("Записаться ещё раз", payload=cb.subbot_open(subbot.id))]]
        await self.sender.send(booking.user_id, body, rows)

    # --- Напоминания -------------------------------------------------------

    async def send_reminder(self, booking_id: int) -> bool:
        """Напоминание клиенту за reminder_hours часов до записи."""
        booking = await self.repos.bookings.get_by_id(booking_id)
        if booking is None or not booking.is_active:
            return False
        if booking.starts_at <= int(time.time()):
            return False

        subbot = await self.repos.subbots.get_by_id(booking.subbot_id)
        if subbot is None or subbot.reminder_hours <= 0:
            return False

        body = text.reminder_client(subbot.title, booking)
        rows: list[Row] = [
            [
                Btn("Отменить", payload=cb.booking_cancel(booking.id)),
                Btn(text.BUTTON_MINE, payload=cb.MENU_MINE),
            ]
        ]
        return await self.sender.send(booking.user_id, body, rows)

    async def send_digest(self, owner_user_id: int, subbot: Subbot) -> bool:
        """Утренняя сводка по подботу: что записано на сегодня."""
        tz = subbot.tz
        today = local_date(int(time.time()), tz)
        day_start = to_unix(localize(today, 0, tz))
        day_end = to_unix(localize(today + timedelta(days=1), 0, tz))

        bookings = await self.repos.bookings.list_subbot_day(subbot.id, day_start, day_end)
        if not bookings:
            await self.sender.send(owner_user_id, text.digest_empty(subbot.title), [])
            return True

        lines = [text.digest_header(subbot.title, len(bookings)), ""]
        for item in bookings:
            who = text.format_values(subbot, item.values).replace("\n", " · ") or "без данных"
            lines.append(f"• {item.start_local:%H:%M}–{item.end_local:%H:%M} — {who}")

        rows: list[Row] = [[Btn("Все записи", payload=cb.subbot_section(subbot.id, cb.SECTION_BOOKINGS))]]
        return await self.sender.send(owner_user_id, "\n".join(lines), rows)

    # --- Отмена с уведомлением обеих сторон --------------------------------

    async def handle_cancel(self, subbot: Subbot, booking: Booking, source: CancelSource) -> None:
        """Гасит фоновую задачу напоминания и уведомляет обе стороны."""
        await self.repos.jobs.cancel(self.repos.jobs.reminder_key(booking.id))
        await self.notify_owner_of_cancel(subbot, booking, source)
        if source is CancelSource.OWNER:
            await self.notify_client_of_cancel(subbot, booking)


__all__ = ["Notifier"]
