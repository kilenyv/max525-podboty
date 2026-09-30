"""Нарезка свободных слотов.

Модуль чистый: работает только с уже загруженными интервалами, перерывами
и занятыми отрезками. Хранить слоты в БД не нужно — они каждый раз
пересчитываются, поэтому расписание и занятость не могут разойтись.

Правила нарезки:
  * слоты выравниваются по началу рабочего интервала, а не по полуночи,
    поэтому 10:00–18:00 при 30-минутном слоте даёт ровно 10:00, 10:30, …;
  * неполный слот в конце интервала не создаётся;
  * слот, пересекающийся с перерывом, выбрасывается целиком;
  * занятость определяется пересечением отрезков, а не сравнением равенства.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from bot.domain.models import DayAvailability, Interval, Slot
from bot.domain.timeutil import (
    MINUTES_IN_DAY,
    to_unix,
    weekday_of,
)


class SlotError(ValueError):
    """Некорректные входные данные для нарезки."""


def _validate(slot_minutes: int) -> None:
    if slot_minutes <= 0 or slot_minutes > MINUTES_IN_DAY:
        raise SlotError(f"Недопустимая длительность слота: {slot_minutes}")
    if MINUTES_IN_DAY % slot_minutes != 0:
        raise SlotError(
            f"Длительность слота {slot_minutes} мин не делит сутки нацело — последний слот дня будет обрезан"
        )


def _raw_slots(
    intervals: Sequence[Interval], breaks: Sequence[Interval], slot_minutes: int
) -> list[tuple[int, int]]:
    """Временные слоты в минутах локального дня, без учёта занятости."""
    _validate(slot_minutes)

    result: list[tuple[int, int]] = []
    for interval in sorted(intervals, key=lambda item: (item.start, item.end)):
        cursor = interval.start
        while cursor + slot_minutes <= interval.end:
            slot_end = cursor + slot_minutes
            if not any(break_.overlaps(cursor, slot_end) for break_ in breaks):
                result.append((cursor, slot_end))
            cursor += slot_minutes
    return result


def count_slots(
    intervals: Sequence[Interval],
    breaks: Sequence[Interval],
    slot_minutes: int,
) -> int:
    """Сколько слотов получится из этих интервалов.

    Нужно админу, чтобы он видел результат до первой реальной записи.
    """
    try:
        return len(_raw_slots(intervals, breaks, slot_minutes))
    except SlotError:
        return 0


def _occupied_windows(busy: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    return [(start, end) for start, end in busy if end > start]


def slots_for_day(
    day: date,
    tz: ZoneInfo,
    workdays: Iterable[int],
    intervals: dict[int, list[Interval]],
    breaks: dict[int, list[Interval]],
    slot_minutes: int,
    busy: Sequence[tuple[int, int]] = (),
) -> list[Slot]:
    """Все слоты локального дня с отметкой занятости.

    Занятость передаётся как список unix-отрезков (start, end) в UTC.
    Прошедшие слоты остаются в списке — их отсекает вызывающий код,
    чтобы админ видел полную картину дня.
    """
    weekday = weekday_of(day)
    if weekday not in set(workdays):
        return []

    day_intervals = intervals.get(weekday, ())
    day_breaks = breaks.get(weekday, ())
    if not day_intervals:
        return []

    occupied = _occupied_windows(busy)

    slots: list[Slot] = []
    for start_min, end_min in _raw_slots(day_intervals, day_breaks, slot_minutes):
        starts_at = to_unix_local(day, start_min, tz)
        ends_at = to_unix_local(day, end_min, tz)
        if ends_at <= starts_at:  # защита от перехода через смену часов
            continue
        taken = any(starts_at < busy_end and ends_at > busy_start for busy_start, busy_end in occupied)
        slots.append(Slot(starts_at=starts_at, ends_at=ends_at, taken=taken))
    return slots


def to_unix_local(day: date, minutes: int, tz: ZoneInfo) -> int:
    from bot.domain.timeutil import localize

    return to_unix(localize(day, minutes, tz))


def build_availability(
    start_day: date,
    days: int,
    tz: ZoneInfo,
    workdays: Iterable[int],
    intervals: dict[int, list[Interval]],
    breaks: dict[int, list[Interval]],
    slot_minutes: int,
    busy: Sequence[tuple[int, int]] = (),
    now_ts: int | None = None,
) -> list[DayAvailability]:
    """Сводка по дням: сколько слотов свободно, сколько всего.

    Дни без единого свободного слота в выдачу не попадают.
    """
    if days <= 0:
        return []

    result: list[DayAvailability] = []
    for offset in range(days):
        day = start_day + timedelta(days=offset)
        slots = slots_for_day(
            day=day,
            tz=tz,
            workdays=workdays,
            intervals=intervals,
            breaks=breaks,
            slot_minutes=slot_minutes,
            busy=busy,
        )
        if not slots:
            continue
        free = sum(1 for slot in slots if not slot.taken)
        if now_ts is not None:
            free = sum(1 for slot in slots if not slot.taken and slot.starts_at > now_ts)
        if free == 0:
            continue
        result.append(DayAvailability(day=day, free=free, total=len(slots)))
    return result


def has_conflict(busy: Sequence[tuple[int, int]], starts_at: int, ends_at: int) -> bool:
    """Есть ли пересечение с любым из занятых отрезков."""
    return any(starts_at < busy_end and ends_at > busy_start for busy_start, busy_end in busy)


def is_within_horizon(starts_at: int, now_ts: int, horizon_days: int) -> bool:
    """Слот не в прошлом и не дальше горизонта записи."""
    if starts_at <= now_ts:
        return False
    if horizon_days <= 0:
        return True
    return starts_at <= now_ts + horizon_days * 24 * 3600


def parse_interval_range(value: str) -> Interval:
    """Разбирает '10:00-18:00' (разделители: дефис, тире, точка с запятой)."""
    from bot.domain.timeutil import hhmm_to_minutes

    normalized = value.strip().replace("—", "-").replace("–", "-")
    for separator in ("-", "..", ";"):
        if separator in normalized:
            left, _, right = normalized.partition(separator)
            break
    else:
        raise ValueError("Укажите интервал в формате 10:00-18:00")

    start = hhmm_to_minutes(left)
    end = hhmm_to_minutes(right)
    return Interval(start=start, end=end)
