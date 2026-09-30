"""Правила записи: часовой пояс, горизонт, лимит отмены, напоминания, дайджест.

Почти все значения — числа из небольшого набора, поэтому основной способ
их задать — кнопка. Рядом с каждым рядом есть кнопка «другое значение»
для тех, кому нужны нестандартные числа.
"""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bot.domain import callback as cb
from bot.domain.models import Subbot
from bot.domain.timeutil import COMMON_TIMEZONES
from bot.handlers.admin.schedule import ask_number
from bot.handlers.base import S_RULES_TZ, AppContext, message_text, user_id_of
from bot.logging_config import get_logger
from bot.services import text
from bot.services.ui import Btn, Row, wrap

log = get_logger(__name__)

#: Ключ раздела в коде кнопки p:<id>:rul:<ключ>:<значение>.
KEY = "rul"

#: Часовые пояса России и соседей — их чаще всего выбирают.
TZ_PRESETS: tuple[tuple[str, str], ...] = (
    ("Europe/Kaliningrad", "Калининград"),
    ("Europe/Moscow", "Москва"),
    ("Europe/Samara", "Самара"),
    ("Asia/Yekaterinburg", "Екатеринбург"),
    ("Asia/Omsk", "Омск"),
    ("Asia/Krasnoyarsk", "Красноярск"),
    ("Asia/Irkutsk", "Иркутск"),
    ("Asia/Yakutsk", "Якутск"),
    ("Asia/Vladivostok", "Владивосток"),
)

#: Ключ → (колонка, минимум, максимум, кнопки, подпись).
NUMERIC_RULES: dict[str, tuple[str, int, int, tuple[int, ...], str]] = {
    "horizon": ("horizon_days", 1, 365, (3, 7, 14, 30, 60, 90), " дн."),
    "notice": ("cancel_notice_hours", 0, 720, (0, 1, 2, 4, 12, 24), " ч"),
    "reminder": ("reminder_hours", 0, 720, (0, 2, 6, 12, 24, 48), " ч"),
    "digest": ("digest_hour", 0, 23, (7, 8, 9, 10, 11), ":00"),
}

#: Подписи для ручного ввода.
NUMERIC_PROMPTS: dict[str, str] = {
    "horizon": "Горизонт записи в днях (1–365):",
    "notice": "За сколько часов клиент может отменить (0–720):",
    "reminder": "Напоминать клиенту за сколько часов (0 — не напоминать):",
    "digest": "Час утренней сводки (0–23):",
}


async def handle_param(ctx: AppContext, event: Any, subbot: Subbot, parts: list[str]) -> bool:
    """Обрабатывает коды правил: p:<id>:<ключ>[:<значение>].

    Возвращает False, если код относится не к разделу правил.
    """
    section = parts[2] if len(parts) > 2 else ""

    if section == "tz":
        if len(parts) < 4:
            return True
        return await _set_timezone(ctx, event, subbot, parts[3])

    if section == "tz_open":
        await _show_timezones(ctx, event, subbot)
        return True

    if section == "tz_manual":
        await ask_timezone(ctx, event, subbot)
        return True

    if section != KEY or len(parts) < 4:
        return False

    key = parts[3]
    if key in NUMERIC_RULES and len(parts) > 4:
        await _set_number(ctx, event, subbot, key, parts[4])
        return True

    # Только «ключ» без значения — просим число текстом.
    if key in NUMERIC_RULES:
        await ask_number(ctx, event, subbot, key, 0, NUMERIC_PROMPTS[key])
        return True

    return True


async def show_rules(ctx: AppContext, event: Any, subbot: Subbot) -> None:
    """Экран правил записи со всеми текущими значениями.

    Строка с названием правила одновременно показывает текущее значение
    и предлагает ввести другое: отдельная кнопка «Другое значение» ради
    одной строки только удлиняла бы экран.
    """
    rows: list[Row] = [
        [Btn(f"🕐 {text.tz_label(subbot.timezone)}", payload=cb.param_set(subbot.id, "tz_open"))],
    ]
    rows.extend(
        wrap(
            [
                Btn(
                    f"{'✅ ' if subbot.timezone == name else ''}{label}",
                    payload=cb.param_set(subbot.id, "tz", name),
                )
                for name, label in TZ_PRESETS
            ],
            3,
        )
    )

    for key, (_column, _low, _high, presets, suffix) in NUMERIC_RULES.items():
        current = _current_value(subbot, key)
        rows.append(
            [Btn(f"{_rule_title(key)}: {current}{suffix} ✏️", payload=cb.param_set(subbot.id, KEY, key))]
        )
        rows.extend(wrap(_presets(subbot.id, key, presets, current, suffix), 3))

    rows.append([Btn("⏱ Длительность встречи", payload=cb.subbot_section(subbot.id, cb.SECTION_SCHEDULE))])
    rows.append([Btn(text.BUTTON_BACK, payload=cb.subbot_card(subbot.id))])

    await ctx.answer(event, text.rules_view(subbot), rows)


# --- Часовые пояса ----------------------------------------------------------


async def _show_timezones(ctx: AppContext, event: Any, subbot: Subbot) -> None:
    """Полный список поясов — их слишком много для основного экрана."""
    buttons = [
        Btn(
            f"{'✅ ' if subbot.timezone == name else ''}{name.replace('_', ' ')}",
            payload=cb.param_set(subbot.id, "tz", name),
        )
        for name in COMMON_TIMEZONES
    ]
    rows = wrap(buttons, 2)
    rows.append([Btn("Ввести вручную", payload=cb.param_set(subbot.id, "tz_manual"))])
    rows.append([Btn(text.BUTTON_BACK, payload=cb.subbot_section(subbot.id, cb.SECTION_RULES))])
    await ctx.answer(event, "Выберите часовой пояс:", rows)


async def ask_timezone(ctx: AppContext, event: Any, subbot: Subbot) -> None:
    """Ждёт название часового пояса, если список не подошёл."""
    user_id = user_id_of(event)
    if user_id is None:
        return

    session = await ctx.session(user_id)
    await session.set_state(S_RULES_TZ, subbot_id=subbot.id)
    await ctx.answer(
        event,
        "Пришлите название часового пояса, например <code>Europe/Kaliningrad</code>.\n\n"
        "Подходят значения вида Region/City из базы IANA: "
        "<code>Asia/Yekaterinburg</code>, <code>Europe/Samara</code> и так далее.",
    )


async def _set_timezone(ctx: AppContext, event: Any, subbot: Subbot, name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        await ctx.answer(event, "Такого часового пояса нет. Выберите из списка.")
        return True

    await ctx.repos.subbots.update_rules(subbot.id, subbot.owner_user_id, timezone=name)
    await show_rules(ctx, event, await _reload(ctx, subbot))
    return True


# --- Числовые правила -------------------------------------------------------


def _presets(subbot_id: int, key: str, presets: tuple[int, ...], current: int, suffix: str) -> list[Btn]:
    return [
        Btn(
            f"{'✅ ' if current == value else ''}{value}{suffix}",
            payload=cb.param_set(subbot_id, KEY, key, str(value)),
        )
        for value in presets
    ]


def _current_value(subbot: Subbot, key: str) -> int:
    return {
        "horizon": subbot.horizon_days,
        "notice": subbot.cancel_notice_hours,
        "reminder": subbot.reminder_hours,
        "digest": subbot.digest_hour,
    }[key]


def _rule_title(key: str) -> str:
    return {
        "horizon": "Горизонт записи",
        "notice": "Минимум до отмены",
        "reminder": "Напоминание клиенту",
        "digest": "Утренняя сводка",
    }[key]


async def _set_number(ctx: AppContext, event: Any, subbot: Subbot, key: str, raw: str) -> None:
    """Сохраняет числовое правило, обрезая значение по допустимому диапазону."""
    column, low, high, _presets_list, _suffix = NUMERIC_RULES[key]
    value = cb.to_int(raw)
    if value is None:
        await ctx.answer(event, "Нужно целое число.")
        return

    await ctx.repos.subbots.update_rules(
        subbot.id, subbot.owner_user_id, **{column: max(low, min(high, value))}
    )
    await show_rules(ctx, event, await _reload(ctx, subbot))


# --- Ввод текстом -----------------------------------------------------------


async def handle_text(ctx: AppContext, event: Any) -> bool:
    """Обрабатывает введённое вручную название часового пояса."""
    user_id = user_id_of(event)
    if user_id is None:
        return False

    session = await ctx.session(user_id)
    if session.state != S_RULES_TZ:
        return False

    subbot = await ctx.repos.subbots.get_by_id(int(session.data.get("subbot_id") or 0))
    if subbot is None or subbot.owner_user_id != user_id:
        await session.finish()
        await ctx.answer(event, "Подбот не найден или он не вас.")
        return True

    await session.finish()
    return await _set_timezone(ctx, event, subbot, message_text(event).strip().replace(" ", "_"))


async def _reload(ctx: AppContext, subbot: Subbot) -> Subbot:
    refreshed = await ctx.repos.subbots.get_by_id(subbot.id)
    return refreshed or subbot


__all__ = ["KEY", "NUMERIC_RULES", "TZ_PRESETS", "handle_param", "handle_text", "show_rules"]
