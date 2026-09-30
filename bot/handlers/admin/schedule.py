"""Редактор расписания: интервалы и перерывы по дням недели.

Логика простая и предсказуемая: бот сам нарезает окно на слоты нужной
длительности от начала интервала, поэтому мастеру достаточно задать
«с какого по какое я работаю» и «когда у меня перерыв».
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from bot.db.repositories.sessions import Session
from bot.domain import callback as cb
from bot.domain.models import Interval, Subbot
from bot.domain.slots import count_slots, parse_interval_range
from bot.domain.timeutil import MINUTES_IN_DAY, WEEKDAYS_RU, WEEKDAYS_SHORT_RU
from bot.handlers.base import (
    S_SCHEDULE_BREAK,
    S_SCHEDULE_NUMBER,
    S_SCHEDULE_RANGE,
    AppContext,
    message_text,
    user_id_of,
)
from bot.logging_config import get_logger
from bot.services import text
from bot.services.ui import Btn, Row, wrap

log = get_logger(__name__)

#: Длительности встречи, которые чаще всего выбирают.
SLOT_PRESETS: tuple[tuple[int, str], ...] = (
    (15, "15 мин"),
    (30, "30 мин"),
    (45, "45 мин"),
    (60, "1 ч"),
    (90, "1 ч 30 мин"),
    (120, "2 ч"),
)


async def handle_param(ctx: AppContext, event: Any, subbot: Subbot, parts: list[str]) -> bool:
    """Обрабатывает p:<id>:<key>[:<value>] для раздела расписания.

    Возвращает False, если код относится не к расписанию.
    """
    key = parts[2]
    value = parts[3] if len(parts) > 3 else ""
    user_id = subbot.owner_user_id

    if key == "slot":
        minutes = cb.to_int(value)
        if minutes is None or minutes <= 0 or MINUTES_IN_DAY % minutes != 0:
            await ctx.answer(event, "Нужна длительность, на которую делятся сутки: 15, 30, 45, 60…")
            return True
        await ctx.repos.subbots.update_rules(subbot.id, user_id, slot_minutes=minutes)
        await show_schedule_overview(ctx, event, await _reload(ctx, subbot))
        return True

    if key == "slot_manual":
        await ask_number(ctx, event, subbot, "slot", 0, "Длительность встречи в минутах:")
        return True

    if key in ("add_iv", "add_br"):
        weekday = cb.to_int(value) or 0
        if not 1 <= weekday <= 7:
            return True
        await ask_range(ctx, event, subbot, weekday, is_break=key == "add_br")
        return True

    if key == "clr":
        weekday = cb.to_int(value) or 0
        if not 1 <= weekday <= 7:
            return True
        await ctx.repos.subbots.clear_day(subbot.id, user_id, weekday)
        await show_day(ctx, event, await _reload(ctx, subbot), weekday)
        return True

    return False


async def handle_interval_delete(ctx: AppContext, event: Any, subbot: Subbot, parts: list[str]) -> bool:
    """Обрабатывает i:<id>:<weekday>:<index> и r:<id>:<weekday>:<index>."""
    is_interval = parts[0] == "i"
    weekday = cb.to_int(parts[2]) if len(parts) > 2 else None
    index = cb.to_int(parts[3]) if len(parts) > 3 else None
    if weekday is None or index is None or not 1 <= weekday <= 7:
        return True

    source = subbot.intervals if is_interval else subbot.breaks
    item = _pick(source.get(weekday), index)
    if item is not None and item.id is not None:
        if is_interval:
            await ctx.repos.subbots.delete_interval(subbot.id, subbot.owner_user_id, weekday, item.id)
        else:
            await ctx.repos.subbots.delete_break(subbot.id, subbot.owner_user_id, weekday, item.id)

    await show_day(ctx, event, await _reload(ctx, subbot), weekday)
    return True


async def handle_text(ctx: AppContext, event: Any) -> bool:
    """Обрабатывает свободный текст в диалогах расписания и правил."""
    user_id = user_id_of(event)
    if user_id is None:
        return False

    session = await ctx.session(user_id)
    if session.state not in (S_SCHEDULE_RANGE, S_SCHEDULE_BREAK, S_SCHEDULE_NUMBER):
        return False

    subbot = await ctx.repos.subbots.get_by_id(int(session.data.get("subbot_id") or 0))
    if subbot is None or subbot.owner_user_id != user_id:
        await session.finish()
        await ctx.answer(event, "Подбот не найден или он не ваш.")
        return True

    raw = message_text(event).strip()
    if not raw:
        return True

    weekday = int(session.data.get("weekday") or 0)

    if session.state == S_SCHEDULE_NUMBER:
        await _handle_number(ctx, event, subbot, session, raw, weekday)
        return True

    is_break = session.state == S_SCHEDULE_BREAK
    try:
        interval = parse_interval_range(raw)
    except ValueError as exc:
        await ctx.answer(event, f"{exc}\n\nПример: <code>10:00-18:00</code>")
        return True

    if interval.end - interval.start < subbot.slot_minutes:
        await ctx.answer(
            event,
            f"Окно короче длительности встречи ({text.format_duration(subbot.slot_minutes)}).",
        )
        return True

    try:
        if is_break:
            await ctx.repos.subbots.add_break(subbot.id, user_id, weekday, interval.start, interval.end)
        else:
            await ctx.repos.subbots.add_interval(subbot.id, user_id, weekday, interval.start, interval.end)
    except PermissionError:
        await session.finish()
        await ctx.answer(event, "Подбот не найден или он не ваш.")
        return True
    except ValueError:
        await ctx.answer(event, "Такой интервал пересекается с уже добавленным.")
        return True

    await session.set_state(None)
    await show_day(ctx, event, await _reload(ctx, subbot), weekday)
    return True


# --- Экраны -----------------------------------------------------------------


async def show_schedule_overview(ctx: AppContext, event: Any, subbot: Subbot) -> None:
    """Список дней недели со сводкой рабочего времени."""
    rows: list[Row] = []
    for weekday in range(1, 8):
        intervals = subbot.intervals.get(weekday, [])
        pauses = subbot.breaks.get(weekday, [])
        label = WEEKDAYS_SHORT_RU[weekday - 1]
        if not intervals:
            rows.append([Btn(f"🔴 {label} — выходной", payload=cb.weekday_pick(subbot.id, weekday))])
            continue

        summary = " · ".join(str(item) for item in intervals)
        if pauses:
            summary += "  (перерыв: " + ", ".join(str(item) for item in pauses) + ")"
        rows.append([Btn(f"🟢 {label} {summary}", payload=cb.weekday_pick(subbot.id, weekday))])

    rows.extend(_slot_length_rows(subbot))
    rows.append([Btn(text.BUTTON_BACK, payload=cb.subbot_card(subbot.id))])

    header = (
        "🗓 <b>Расписание</b>\n\n"
        "Выберите день недели и задайте рабочее время. "
        f"Бот сам нарежет окно на слоты по {text.format_duration(subbot.slot_minutes)}."
    )
    if not subbot.is_configured:
        header += "\n\n⚠️ Пока нет ни одного рабочего дня — клиенты не смогут записаться."
    await ctx.answer(event, header, rows)


def _slot_length_rows(subbot: Subbot) -> list[Row]:
    rows = [
        [
            Btn(
                f"{'✅ ' if subbot.slot_minutes == minutes else ''}{label}",
                payload=cb.param_set(subbot.id, "slot", str(minutes)),
            )
            for minutes, label in SLOT_PRESETS
        ]
    ]
    rows.append(
        [
            Btn(
                "Другая длительность",
                payload=cb.param_set(subbot.id, "slot_manual"),
            )
        ]
    )
    return rows


async def show_day(ctx: AppContext, event: Any, subbot: Subbot, weekday: int) -> None:
    """Экран одного дня: интервалы, перерывы и кнопки добавления."""
    intervals = subbot.intervals.get(weekday, [])
    pauses = subbot.breaks.get(weekday, [])
    name = WEEKDAYS_RU[weekday - 1]

    lines = [f"<b>{name}</b>", ""]
    if intervals:
        lines.append("Рабочее время:")
        for item in intervals:
            lines.append(f"  · {item}")
    else:
        lines.append("Рабочее время не задано.")

    if pauses:
        lines.append("")
        lines.append("Перерывы:")
        for item in pauses:
            lines.append(f"  · {item}")

    lines.append("")
    lines.append(f"Длительность встречи: {text.format_duration(subbot.slot_minutes)}")
    total = count_slots(
        subbot.intervals.get(weekday, []), subbot.breaks.get(weekday, []), subbot.slot_minutes
    )
    if total:
        lines.append(f"Слотов на этот день: {total}")

    rows: list[Row] = []
    if intervals:
        rows.extend(
            wrap(
                [
                    Btn(f"🗑 {item}", payload=cb.interval_delete(subbot.id, weekday, index))
                    for index, item in enumerate(intervals)
                ],
                3,
            )
        )
    if pauses:
        rows.extend(
            wrap(
                [
                    Btn(f"🗑 {item}", payload=cb.break_delete(subbot.id, weekday, index))
                    for index, item in enumerate(pauses)
                ],
                3,
            )
        )

    rows.append(
        [
            Btn("➕ Рабочее время", payload=cb.param_set(subbot.id, "add_iv", str(weekday))),
            Btn("➕ Перерыв", payload=cb.param_set(subbot.id, "add_br", str(weekday))),
        ]
    )
    rows.append(
        [
            Btn("🗑 Очистить день", payload=cb.param_set(subbot.id, "clr", str(weekday))),
            Btn(text.BUTTON_BACK, payload=cb.subbot_section(subbot.id, cb.SECTION_SCHEDULE)),
        ]
    )

    await ctx.answer(event, "\n".join(lines), rows)


# --- Ввод чисел и интервалов ---


async def _handle_number(
    ctx: AppContext, event: Any, subbot: Subbot, session: Session, raw: str, weekday: int
) -> None:
    """Ввод числа: длительность слота либо другое правило."""
    key = str(session.data.get("key") or "")
    try:
        value = int(raw)
    except ValueError:
        await ctx.answer(event, "Нужно целое число.")
        return

    user_id = subbot.owner_user_id

    if key == "slot":
        if value <= 0 or MINUTES_IN_DAY % value != 0:
            await ctx.answer(event, "Нужна длительность, на которую делятся сутки: 15, 30, 45, 60…")
            return
        await ctx.repos.subbots.update_rules(subbot.id, user_id, slot_minutes=value)
        target = "schedule"
    elif key == "horizon":
        if not 1 <= value <= 365:
            await ctx.answer(event, "Горизонт записи — от 1 до 365 дней.")
            return
        await ctx.repos.subbots.update_rules(subbot.id, user_id, horizon_days=value)
        target = "rules"
    elif key == "notice":
        if not 0 <= value <= 720:
            await ctx.answer(event, "Лимит отмены — от 0 до 720 часов.")
            return
        await ctx.repos.subbots.update_rules(subbot.id, user_id, cancel_notice_hours=value)
        target = "rules"
    elif key == "reminder":
        if not 0 <= value <= 720:
            await ctx.answer(event, "Напоминание — от 0 (выключить) до 720 часов.")
            return
        await ctx.repos.subbots.update_rules(subbot.id, user_id, reminder_hours=value)
        target = "rules"
    elif key == "digest":
        if not 0 <= value <= 23:
            await ctx.answer(event, "Час дайджеста — от 0 до 23.")
            return
        await ctx.repos.subbots.update_rules(subbot.id, user_id, digest_hour=value)
        target = "rules"
    else:
        await session.finish()
        return

    await session.finish()
    refreshed = await _reload(ctx, subbot)

    if target == "rules":
        from bot.handlers.admin.rules import show_rules

        await show_rules(ctx, event, refreshed)
    elif target == "schedule":
        await show_schedule_overview(ctx, event, refreshed)
    else:
        await show_day(ctx, event, refreshed, weekday)


async def ask_range(ctx: AppContext, event: Any, subbot: Subbot, weekday: int, is_break: bool) -> None:
    """Просит прислать интервал и запоминает, что именно настраиваем."""
    user_id = user_id_of(event)
    if user_id is None:
        return

    session = await ctx.session(user_id)
    await session.set_state(
        S_SCHEDULE_BREAK if is_break else S_SCHEDULE_RANGE,
        subbot_id=subbot.id,
        weekday=weekday,
    )
    what = "Перерыв" if is_break else "Рабочее время"
    await ctx.answer(event, text.ask_time_range(what))


async def ask_number(ctx: AppContext, event: Any, subbot: Subbot, key: str, weekday: int, what: str) -> None:
    """Просит прислать число для настройки."""
    user_id = user_id_of(event)
    if user_id is None:
        return

    session = await ctx.session(user_id)
    await session.set_state(S_SCHEDULE_NUMBER, subbot_id=subbot.id, key=key, weekday=weekday)
    await ctx.answer(event, text.ask_number(what))


# --- Разбор кодов -----------------------------------------------------------


def _pick(items: Sequence[Interval] | None, index: int) -> Interval | None:
    if items is None or not 0 <= index < len(items):
        return None
    return items[index]


async def _reload(ctx: AppContext, subbot: Subbot) -> Subbot:
    refreshed = await ctx.repos.subbots.get_by_id(subbot.id)
    return refreshed or subbot


__all__ = [
    "SLOT_PRESETS",
    "ask_number",
    "ask_range",
    "handle_interval_delete",
    "handle_param",
    "handle_text",
    "show_day",
    "show_schedule_overview",
]
