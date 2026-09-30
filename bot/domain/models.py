"""Модели доменного слоя."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

from bot.domain.enums import BookingStatus, FieldType
from bot.domain.timeutil import get_tz, to_unix


@dataclass(frozen=True, slots=True)
class Interval:
    """Рабочий интервал или перерыв. Минуты от полуночи локального дня."""

    start: int
    end: int
    id: int | None = None

    def __post_init__(self) -> None:
        if self.start >= self.end:
            raise ValueError(f"Некорректный интервал: {self.start}-{self.end}")

    def overlaps(self, start: int, end: int) -> bool:
        return start < self.end and end > self.start

    def __str__(self) -> str:
        from bot.domain.timeutil import minutes_to_hhmm

        return f"{minutes_to_hhmm(self.start)}-{minutes_to_hhmm(self.end)}"


@dataclass(frozen=True, slots=True)
class FormField:
    """Поле формы записи: что клиент вводит и обязательно ли."""

    id: int
    subbot_id: int
    field_type: FieldType
    label: str
    is_required: bool
    position: int
    max_length: int


@dataclass(slots=True)
class Subbot:
    """Подбот — страница записи одного мастера или услуги."""

    id: int
    owner_user_id: int
    slug: str
    title: str
    description: str
    timezone: str
    slot_minutes: int
    horizon_days: int
    cancel_notice_hours: int
    reminder_hours: int
    digest_hour: int
    is_active: bool
    created_at: int
    updated_at: int
    workdays: set[int] = field(default_factory=set)
    intervals: dict[int, list[Interval]] = field(default_factory=dict)
    breaks: dict[int, list[Interval]] = field(default_factory=dict)
    form_fields: list[FormField] = field(default_factory=list)

    @property
    def tz(self) -> ZoneInfo:
        return get_tz(self.timezone)

    @property
    def is_configured(self) -> bool:
        """Готов ли подбот принимать записи."""
        return bool(self.workdays) and any(self.intervals.get(day) for day in self.workdays)

    @property
    def required_fields(self) -> list[FormField]:
        return [f for f in self.form_fields if f.is_required]


@dataclass(frozen=True, slots=True)
class Booking:
    """Запись клиента на слот подбота."""

    id: int
    subbot_id: int
    user_id: int
    starts_at: int
    ends_at: int
    timezone: str
    status: BookingStatus
    created_at: int
    updated_at: int
    cancelled_at: int | None = None
    cancel_source: str | None = None
    values: dict[int, str] = field(default_factory=dict)

    @property
    def tz(self) -> ZoneInfo:
        return get_tz(self.timezone)

    @property
    def start_local(self) -> datetime:
        return datetime.fromtimestamp(self.starts_at, tz=self.tz)

    @property
    def end_local(self) -> datetime:
        return datetime.fromtimestamp(self.ends_at, tz=self.tz)

    @property
    def is_active(self) -> bool:
        return self.status is BookingStatus.ACTIVE


@dataclass(frozen=True, slots=True)
class Slot:
    """Свободный или занятый слот в локальном времени подбота."""

    starts_at: int  # unix, UTC
    ends_at: int  # unix, UTC
    taken: bool = False

    @property
    def start_local(self) -> datetime:
        return datetime.fromtimestamp(self.starts_at, tz=ZoneInfo("UTC"))

    def local_day(self, tz: ZoneInfo) -> date:
        return datetime.fromtimestamp(self.starts_at, tz=tz).date()

    def overlaps_range(self, start: int, end: int) -> bool:
        return self.starts_at < end and self.ends_at > start


@dataclass(frozen=True, slots=True)
class DayAvailability:
    """Сводка по одному дню для отрисовки кнопок выбора даты."""

    day: date
    free: int
    total: int

    @property
    def has_free(self) -> bool:
        return self.free > 0


def intervals_for(intervals: dict[int, list[Interval]], weekday: int) -> Sequence[Interval]:
    return intervals.get(weekday, ())


def breaks_for(breaks: dict[int, list[Interval]], weekday: int) -> Sequence[Interval]:
    return breaks.get(weekday, ())


def slot_length_seconds(slot_minutes: int) -> int:
    return slot_minutes * 60


def start_of_day_unix(day: date, tz: ZoneInfo) -> int:
    return to_unix(datetime.combine(day, datetime.min.time(), tzinfo=tz))
