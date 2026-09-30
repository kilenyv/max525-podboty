"""Нарезка слотов: главное правило — слоты идут от начала интервала."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from bot.domain.models import Interval
from bot.domain.slots import (
    SlotError,
    build_availability,
    count_slots,
    has_conflict,
    parse_interval_range,
    slots_for_day,
)
from bot.domain.timeutil import hhmm_to_minutes, weekday_of

MSK = ZoneInfo("Europe/Moscow")

#: Понедельник: все тесты считают неделю с первого дня.
DAY = date(2026, 9, 7)
assert weekday_of(DAY) == 1, "в тестах нужен понедельник"


def iv(start: str, end: str) -> Interval:
    return Interval(start=hhmm_to_minutes(start), end=hhmm_to_minutes(end))


def day_slots(day: date = DAY, **overrides):
    """Слоты дня с настройками по умолчанию: пн 10:00–18:00, шаг час."""
    params = {
        "day": day,
        "tz": MSK,
        "workdays": [1],
        "intervals": {1: [iv("10:00", "18:00")]},
        "breaks": {1: []},
        "slot_minutes": 60,
    }
    params.update(overrides)
    return slots_for_day(**params)


def labels(found, tz=MSK) -> list[str]:
    return [f"{datetime.fromtimestamp(slot.starts_at, tz=tz):%H:%M}" for slot in found]


class TestAlignment:
    def test_slots_start_at_interval_start_not_midnight(self) -> None:
        assert labels(day_slots())[:3] == ["10:00", "11:00", "12:00"]

    def test_second_interval_restarts_alignment(self) -> None:
        found = day_slots(intervals={1: [iv("10:00", "12:00"), iv("14:00", "16:00")]})
        assert labels(found) == ["10:00", "11:00", "14:00", "15:00"]

    def test_partial_slot_at_the_end_is_dropped(self) -> None:
        # 10:00–18:30 при часовом слоте: 18:00 не помещается целиком.
        assert count_slots([iv("10:00", "18:30")], [], 60) == 8

    def test_uneven_slot_length_is_rejected(self) -> None:
        # 50 минут не делят сутки: последний слот дня был бы обрезан.
        with pytest.raises(SlotError):
            day_slots(slot_minutes=50)

    def test_count_slots_swallows_bad_length(self) -> None:
        # Админ должен увидеть «0», а не исключение в обработчике кнопки.
        assert count_slots([iv("10:00", "18:00")], [], 50) == 0


class TestBreaks:
    def test_slot_overlapping_break_is_dropped_whole(self) -> None:
        # Перерыв 11:30–12:30 выбрасывает слоты 11:00 и 12:00 целиком.
        assert labels(day_slots(breaks={1: [iv("11:30", "12:30")]})) == [
            "10:00",
            "13:00",
            "14:00",
            "15:00",
            "16:00",
            "17:00",
        ]

    def test_break_touching_slot_boundary_keeps_slot(self) -> None:
        # 12:00–13:00 касается, но не пересекает слот 11:00–12:00.
        assert labels(day_slots(breaks={1: [iv("12:00", "13:00")]})) == [
            "10:00",
            "11:00",
            "13:00",
            "14:00",
            "15:00",
            "16:00",
            "17:00",
        ]

    def test_several_breaks_per_day(self) -> None:
        found = day_slots(slot_minutes=120, breaks={1: [iv("12:00", "13:00"), iv("16:00", "16:30")]})
        assert labels(found) == ["10:00", "14:00"]


class TestBusy:
    def test_busy_marks_exact_match(self) -> None:
        reference = day_slots()
        found = day_slots(busy=[(reference[2].starts_at, reference[2].ends_at)])
        assert [slot.taken for slot in found] == [False, False, True, False, False, False, False, False]

    def test_partial_overlap_also_counts_as_busy(self) -> None:
        reference = day_slots()
        # Занято 10:30–11:30: пересекает слоты 10:00 и 11:00.
        found = day_slots(busy=[(reference[0].starts_at + 1800, reference[1].ends_at - 1800)])
        assert [slot.taken for slot in found][:3] == [True, True, False]

    def test_has_conflict_uses_interval_intersection(self) -> None:
        assert has_conflict([(100, 200)], 150, 250) is True
        assert has_conflict([(100, 200)], 200, 300) is False
        assert has_conflict([(100, 200)], 50, 100) is False
        assert has_conflict([], 100, 200) is False


class TestNonWorkingDays:
    def test_day_outside_workdays_gives_no_slots(self) -> None:
        assert day_slots(workdays=[2]) == []

    def test_empty_interval_gives_no_slots(self) -> None:
        assert day_slots(intervals={1: []}) == []


class TestAvailability:
    def _base(self, **overrides):
        params = {
            "start_day": DAY,
            "days": 7,
            "tz": MSK,
            "workdays": [1, 2],
            "intervals": {1: [iv("10:00", "12:00")], 2: [iv("10:00", "12:00")]},
            "breaks": {},
            "slot_minutes": 60,
        }
        params.update(overrides)
        return build_availability(**params)

    def test_only_days_with_intervals_appear(self) -> None:
        found = self._base()
        assert [item.day.isoformat() for item in found] == ["2026-09-07", "2026-09-08"]
        assert [item.free for item in found] == [2, 2]

    def test_fully_busy_day_disappears(self) -> None:
        found = self._base(days=1, busy=[(0, 2**31)])
        assert found == []

    def test_past_slots_do_not_count_as_free(self) -> None:
        # 10:30: слот 10:00 в прошлом, 11:00 ещё доступен.
        found = self._base(days=1, now_ts=day_slots()[0].starts_at + 1800)
        assert [item.free for item in found] == [1]

    def test_day_with_only_past_slots_disappears(self) -> None:
        found = self._base(days=1, now_ts=day_slots()[1].ends_at)
        assert found == []

    def test_zero_days_returns_empty(self) -> None:
        assert build_availability(DAY, 0, MSK, [1], {1: [iv("10:00", "12:00")]}, {}, 60) == []


class TestParseRange:
    @pytest.mark.parametrize(
        "raw",
        [
            "10:00-18:00",
            "10:00–18:00",
            "10:00—18:00",
            "10:00 .. 18:00",
            "10:00;18:00",
            "  10:00 - 18:00  ",
        ],
    )
    def test_accepts_common_separators(self, raw: str) -> None:
        assert parse_interval_range(raw) == Interval(start=600, end=1080)

    @pytest.mark.parametrize("raw", ["10:00", "abc-def", ""])
    def test_rejects_garbage(self, raw: str) -> None:
        with pytest.raises(ValueError):
            parse_interval_range(raw)
