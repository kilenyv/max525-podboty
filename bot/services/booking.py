"""Сценарии записи: доступные дни, слоты, валидация полей, бронирование.

Обработчики не считают слоты сами — только этот модуль знает, как
превратить расписание подбота в конкретное время.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from bot.db.connection import Database
from bot.db.repositories import Repositories
from bot.db.repositories.bookings import SlotUnavailableError
from bot.domain.enums import MAX_TEXT_LENGTH, BookingStatus, CancelSource, FieldType
from bot.domain.models import Booking, DayAvailability, Slot, Subbot
from bot.domain.phone import normalize_phone
from bot.domain.slots import build_availability, has_conflict, slots_for_day
from bot.domain.timeutil import WEEKDAYS_RU, hhmm_to_minutes, localize, to_unix
from bot.logging_config import get_logger

log = get_logger(__name__)


class FieldValidationError(ValueError):
    """Значение поля не прошло проверку."""


@dataclass(frozen=True, slots=True)
class BookingContext:
    """Всё, что нужно показать клиенту по конкретной записи."""

    subbot: Subbot
    day: date
    slots: list[Slot]
    values: dict[int, str]


class BookingService:
    """Слоты, запись и проверка полей формы.

    Здесь собираются вместе расписание подбота, занятые записи и
    правила — всё, что нужно, чтобы ответить на вопрос «можно ли
    записаться на это время» и «что показывать клиенту».
    """

    def __init__(self, db: Database, repos: Repositories) -> None:
        self.db = db
        self.repos = repos

    # --- Выбор дня и слотов ------------------------------------------------

    async def available_days(
        self, subbot: Subbot, user_id: int, now: int | None = None
    ) -> list[DayAvailability]:
        """Дни с хотя бы одним свободным слотом в пределах горизонта записи."""
        now = int(time.time()) if now is None else now
        if not subbot.is_configured or not subbot.is_active:
            return []

        tz = subbot.tz
        today = local_date(now, tz)
        horizon_end = now + max(1, subbot.horizon_days) * 24 * 3600
        busy = await self.repos.bookings.busy_windows(subbot.id, now - 24 * 3600, horizon_end + 24 * 3600)

        return build_availability(
            start_day=today,
            days=max(1, subbot.horizon_days),
            tz=tz,
            workdays=subbot.workdays,
            intervals=subbot.intervals,
            breaks=subbot.breaks,
            slot_minutes=subbot.slot_minutes,
            busy=busy,
            now_ts=now,
        )

    async def day_slots(self, subbot: Subbot, day: date, now: int | None = None) -> list[Slot]:
        """Все слоты дня, включая занятые. Клиенту показываем и те, и другие."""
        now = int(time.time()) if now is None else now
        tz = subbot.tz
        day_start = to_unix(localize(day, 0, tz))
        day_end = to_unix(localize(day + timedelta(days=1), 0, tz))
        busy = await self.repos.bookings.busy_windows(subbot.id, day_start, day_end)

        return slots_for_day(
            day=day,
            tz=tz,
            workdays=subbot.workdays,
            intervals=subbot.intervals,
            breaks=subbot.breaks,
            slot_minutes=subbot.slot_minutes,
            busy=busy,
        )

    async def resolve_slot(
        self, subbot: Subbot, day: date, start_minute: int, now: int | None = None
    ) -> Slot | None:
        """Находит конкретный слот и проверяет, что он свободен и в будущем."""
        now = int(time.time()) if now is None else now
        tz = subbot.tz
        starts_at = to_unix(localize(day, start_minute, tz))

        if starts_at <= now:
            return None

        horizon_end = now + max(1, subbot.horizon_days) * 24 * 3600
        if starts_at > horizon_end:
            return None

        for slot in await self.day_slots(subbot, day, now=now):
            if slot.starts_at == starts_at:
                return None if slot.taken else slot
        return None

    async def is_slot_free(
        self, subbot: Subbot, starts_at: int, ends_at: int, now: int | None = None
    ) -> bool:
        now = int(time.time()) if now is None else now
        if starts_at <= now:
            return False
        if starts_at > now + max(1, subbot.horizon_days) * 24 * 3600:
            return False

        day_start = starts_at - 24 * 3600
        busy = await self.repos.bookings.busy_windows(subbot.id, day_start, starts_at + 24 * 3600)
        return not has_conflict(busy, starts_at, ends_at)

    # --- Поля формы --------------------------------------------------------

    def validate_field(self, subbot: Subbot, field, raw: str) -> str:  # noqa: ANN001
        """Проверяет и нормализует ввод клиента. Бросает FieldValidationError."""
        value = (raw or "").strip()

        if not value:
            if field.is_required:
                raise FieldValidationError("EMPTY")
            return ""

        if field.field_type is FieldType.PHONE:
            normalized = normalize_phone(value)
            if normalized is None:
                raise FieldValidationError("PHONE")
            return normalized

        max_length = field.max_length or 255
        if field.field_type is FieldType.COMMENT:
            max_length = min(max_length, MAX_TEXT_LENGTH)
        if field.field_type is FieldType.TEXT:
            max_length = min(max_length, MAX_TEXT_LENGTH)

        if len(value) > max_length:
            raise FieldValidationError("TOO_LONG")

        if field.field_type in (FieldType.FIRST_NAME, FieldType.LAST_NAME) and len(value) > 64:
            raise FieldValidationError("TOO_LONG")

        return value

    def missing_required(self, subbot: Subbot, values: dict[int, str]) -> list:
        return [
            field
            for field in subbot.form_fields
            if field.is_required and not (values.get(field.id) or "").strip()
        ]

    # --- Создание и отмена -------------------------------------------------

    async def create_booking(
        self,
        subbot: Subbot,
        user_id: int,
        starts_at: int,
        ends_at: int,
        values: dict[int, str],
        now: int | None = None,
    ) -> Booking:
        """Бронирует слот. Бросает SlotUnavailableError, если время уже занято."""
        duplicate = await self.repos.bookings.get_active_by_user_slot(subbot.id, user_id, starts_at)
        if duplicate is not None:
            raise SlotUnavailableError("Вы уже записаны на это время")

        if not await self.is_slot_free(subbot, starts_at, ends_at, now=now):
            raise SlotUnavailableError("Время уже занято")

        return await self.repos.bookings.create(
            subbot_id=subbot.id,
            user_id=user_id,
            starts_at=starts_at,
            ends_at=ends_at,
            timezone=subbot.timezone,
            field_values=values,
            now=now,
        )

    async def cancel_by_client(self, subbot: Subbot, booking: Booking) -> Booking | None:
        """Отмена клиентом с проверкой лимита по времени."""
        now = int(time.time())
        if booking.starts_at - now < subbot.cancel_notice_hours * 3600:
            raise PermissionError(f"NOTICE:{subbot.cancel_notice_hours}")
        return await self.repos.bookings.cancel(booking.id, CancelSource.CLIENT, now=now)

    async def cancel_by_owner(self, booking: Booking) -> Booking | None:
        """Отмена владельцем без ограничения по времени."""
        return await self.repos.bookings.cancel(booking.id, CancelSource.OWNER)

    # --- Представления -----------------------------------------------------

    @staticmethod
    def parse_slot_time(text: str) -> int:
        return hhmm_to_minutes(text)

    @staticmethod
    def day_by_offset(subbot: Subbot, offset: int, now: int | None = None) -> date:
        now = int(time.time()) if now is None else now
        return local_date(now, subbot.tz) + timedelta(days=max(0, offset))

    def weekday_name(self, weekday: int) -> str:
        return WEEKDAYS_RU[max(1, min(7, weekday)) - 1]

    async def close_past_bookings(self) -> int:
        """Переводит завершившиеся записи в статус «выполнено»."""
        return await self.repos.bookings.mark_done(int(time.time()))

    def booking_is_active(self, booking: Booking) -> bool:
        return booking.status is BookingStatus.ACTIVE


def day_by_offset(subbot: Subbot, offset: int, now: int | None = None) -> date:
    return BookingService.day_by_offset(subbot, offset, now=now)


def local_date(now: int, tz) -> date:  # noqa: ANN001 - ZoneInfo
    """Локальная дата по unix-времени в часовом поясе подбота."""
    return datetime.fromtimestamp(now, tz=tz).date()
