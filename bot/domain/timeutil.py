"""Работа с локальным временем и часовыми поясами.

В БД всё хранится в unix-секундах UTC. Локальное время используется только
для отображения и для вычисления границ рабочего дня.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MINUTES_IN_DAY = 24 * 60

WEEKDAYS_RU: tuple[str, ...] = (
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
)

WEEKDAYS_SHORT_RU: tuple[str, ...] = (
    "Пн",
    "Вт",
    "Ср",
    "Чт",
    "Пт",
    "Сб",
    "Вс",
)

MONTHS_SHORT_RU: tuple[str, ...] = (
    "янв",
    "фев",
    "мар",
    "апр",
    "мая",
    "июн",
    "июл",
    "авг",
    "сен",
    "окт",
    "ноя",
    "дек",
)

#: Часовые пояса, которые чаще всего нужны мастерам.
COMMON_TIMEZONES: tuple[str, ...] = (
    "Europe/Kaliningrad",
    "Europe/Moscow",
    "Europe/Samara",
    "Asia/Yekaterinburg",
    "Asia/Omsk",
    "Asia/Krasnoyarsk",
    "Asia/Irkutsk",
    "Asia/Yakutsk",
    "Asia/Vladivostok",
    "Asia/Magadan",
    "Asia/Kamchatka",
)


def is_valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return False
    return True


def get_tz(name: str) -> ZoneInfo:
    """Возвращает ZoneInfo, откатываясь на Europe/Moscow при мусоре в БД."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return ZoneInfo("Europe/Moscow")


def weekday_of(day: date) -> int:
    """ISO-номер дня недели: 1 = понедельник, 7 = воскресенье."""
    return day.isoweekday()


def minutes_to_hhmm(minutes: int) -> str:
    minutes = max(0, min(MINUTES_IN_DAY, minutes))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def hhmm_to_minutes(value: str) -> int:
    """Разбирает 'HH:MM' в минуты от полуночи. Бросает ValueError при мусоре."""
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Неверный формат времени: {value!r}. Ожидается ЧЧ:ММ")
    hours, minutes = int(parts[0]), int(parts[1])
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise ValueError(f"Время вне диапазона: {value!r}")
    return hours * 60 + minutes


def localize(day: date, minutes: int, tz: ZoneInfo) -> datetime:
    """Собирает локальный datetime из даты и минут от полуночи.

    Для неоднозначного времени (переход на летнее время) берётся первое
    вхождение — fold=0, чтобы слот не «прыгал» назад при смене часов.
    """
    minutes = max(0, min(MINUTES_IN_DAY - 1, minutes))
    return datetime(day.year, day.month, day.day, minutes // 60, minutes % 60, tzinfo=tz)


def to_utc(day: date, minutes: int, tz: ZoneInfo) -> datetime:
    return localize(day, minutes, tz).astimezone(ZoneInfo("UTC"))


def now_utc() -> datetime:
    return datetime.now(tz=ZoneInfo("UTC"))


def to_unix(moment: datetime) -> int:
    return int(moment.timestamp())


def from_unix(timestamp: int) -> datetime:
    return datetime.fromtimestamp(timestamp, tz=ZoneInfo("UTC"))


def local_now(tz: ZoneInfo) -> datetime:
    return datetime.now(tz=tz)


def format_date_ru(day: date, with_weekday: bool = True) -> str:
    """'пн, 1 окт' — компактно для кнопок."""
    base = f"{day.day} {MONTHS_SHORT_RU[day.month - 1]}"
    if with_weekday:
        return f"{WEEKDAYS_SHORT_RU[day.weekday()]}, {base}"
    return base


def format_datetime_ru(moment: datetime) -> str:
    return f"{format_date_ru(moment.date())} в {moment.strftime('%H:%M')}"


def describe_range_ru(day: date, tz: ZoneInfo) -> str:
    """'пн, 1 окт, 10:00–18:00 (Europe/Moscow)' для карточки подбота."""
    tz_label = tz.key.replace("_", " ")
    return f"{format_date_ru(day)}, 10:00–18:00 ({tz_label})"


def add_days(day: date, days: int) -> date:
    return day + timedelta(days=days)


def day_bounds_utc(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Начало и конец локальных суток в UTC."""
    start = localize(day, 0, tz).astimezone(ZoneInfo("UTC"))
    end = localize(day + timedelta(days=1), 0, tz).astimezone(ZoneInfo("UTC"))
    return start, end
