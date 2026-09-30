"""Клиентский сценарий записи: расписание → слот → форма → подтверждение.

Состояние диалога живёт в сессии пользователя (таблица sessions), поэтому
шаг выбора формы переживает перезапуск и не требует длинных кодов кнопок.
"""

from __future__ import annotations

import time
from datetime import date, datetime
from typing import Any

from maxapi.types import MessageCallback

from bot.db.repositories.bookings import SlotUnavailableError
from bot.db.repositories.sessions import Session
from bot.domain import callback as cb
from bot.domain.models import FormField, Slot, Subbot
from bot.domain.timeutil import format_date_ru
from bot.filters import is_code, starts_with
from bot.handlers.base import (
    S_CLIENT_COMMENT,
    S_CLIENT_FIELD,
    AppContext,
    callback_payload,
    is_comment_field,
    message_text,
    user_id_of,
    validate_client_field,
    wrap,
)
from bot.logging_config import get_logger
from bot.services import text
from bot.services.booking import day_by_offset, local_date
from bot.services.ui import Btn, Row

log = get_logger(__name__)

#: Сколько кнопок-слотов помещается в ряд.
SLOTS_PER_ROW = 3
#: Сколько кнопок-дней помещается в ряд.
DAYS_PER_ROW = 3
#: Префикс кнопки занятого слота — по нажатию объясняем, что он занят.
TAKEN_PREFIX = "· "


async def register_client_booking(dp, ctx: AppContext) -> None:  # noqa: ANN001
    """Регистрирует клиентские обработчики выбора и записи."""

    @dp.message_callback(is_code(cb.MENU_BOOK))
    async def on_book(event: MessageCallback) -> None:
        await ctx.show_booking_start(event)

    @dp.message_callback(starts_with("s:"))
    async def on_subbot_open(event: MessageCallback) -> None:
        subbot_id = _arg_int(event, 1)
        if subbot_id is None:
            return

        subbot = await _require_subbot(ctx, subbot_id)
        if subbot is None:
            await ctx.answer(
                event,
                text.NOT_A_SUBBOT,
                [[Btn(text.BUTTON_MENU, payload=cb.MENU_HOME)]],
            )
            return

        await _show_days(ctx, event, subbot)

    @dp.message_callback(starts_with("d:"))
    async def on_day_pick(event: MessageCallback) -> None:
        parts = cb.parse_callback(callback_payload(event))
        if len(parts) < 3:
            return

        subbot_id = cb.to_int(parts[1])
        offset = cb.to_int(parts[2])
        if subbot_id is None or offset is None:
            return

        subbot = await _require_subbot(ctx, subbot_id)
        if subbot is None:
            return

        if not _within_horizon(subbot, offset):
            # Старая кнопка из прежнего окна или подделанный код: день
            # за пределами горизонта нельзя открыть, даже если слоты есть.
            await ctx.answer(
                event,
                text.out_of_horizon(subbot),
                [[Btn("Выбрать актуальное время", payload=cb.subbot_open(subbot.id))]],
            )
            return

        await _show_slots(ctx, event, subbot, offset)

    @dp.message_callback(starts_with("t:"))
    async def on_slot_pick(event: MessageCallback) -> None:
        user_id = user_id_of(event)
        parts = cb.parse_callback(callback_payload(event))
        if user_id is None or len(parts) < 4:
            return

        subbot_id = cb.to_int(parts[1])
        offset = cb.to_int(parts[2])
        if subbot_id is None or offset is None:
            return

        subbot = await _require_subbot(ctx, subbot_id)
        if subbot is None or not _within_horizon(subbot, offset):
            return

        session = await ctx.session(user_id)
        day = day_by_offset(subbot, offset)
        start_minute = cb.to_int(parts[3])
        if start_minute is None:
            return

        slot = await ctx.booking.resolve_slot(subbot, day, start_minute)
        if slot is None:
            # Время могли занять, пока клиент выбирал: показываем день заново.
            await _show_slots(ctx, event, subbot, offset)
            return

        await _start_form(ctx, event, subbot, session, day, slot)

    @dp.message_callback(is_code(cb.field_form_finish()))
    async def on_comment_finish(event: MessageCallback) -> None:
        """Кнопка «Готово» завершает комментарий или доп. текст."""
        user_id = user_id_of(event)
        if user_id is None:
            return

        session = await ctx.session(user_id)
        if session.state != S_CLIENT_COMMENT:
            return

        subbot = await ctx.repos.subbots.get_by_id(int(session.data.get("subbot_id") or 0))
        if subbot is None:
            await session.finish()
            return

        if not await _commit_pending(ctx, event, subbot, session):
            return
        await _advance(ctx, event, subbot, session)

    @dp.message_callback(is_code(cb.BOOKING_CONFIRM))
    async def on_confirm(event: MessageCallback) -> None:
        await _confirm_booking(ctx, event)


async def handle_form_input(ctx: AppContext, event: Any) -> bool:
    """Обрабатывает введённое значение поля формы.

    Возвращает False, если пользователь не находится в диалоге записи —
    тогда текст можно отдать другим сценариям.
    """
    user_id = user_id_of(event)
    if user_id is None:
        return False

    session = await ctx.session(user_id)
    if session.state not in (S_CLIENT_FIELD, S_CLIENT_COMMENT):
        return False

    subbot = await ctx.repos.subbots.get_by_id(int(session.data.get("subbot_id") or 0))
    if subbot is None:
        await session.finish()
        await ctx.answer(
            event,
            "Страница записи больше не существует.",
            [[Btn(text.BUTTON_MENU, payload=cb.MENU_HOME)]],
        )
        return True

    await _consume_field(ctx, event, subbot, session)
    return True


# --- Подтверждение ----------------------------------------------------------


async def _confirm_booking(ctx: AppContext, event: MessageCallback) -> None:
    """Создаёт запись из собранных данных сессии."""
    user_id = user_id_of(event)
    if user_id is None:
        return

    session = await ctx.session(user_id)
    subbot = await ctx.repos.subbots.get_by_id(int(session.data.get("subbot_id") or 0))
    if subbot is None:
        await session.finish()
        await ctx.answer(
            event,
            text.NOT_A_SUBBOT,
            [[Btn(text.BUTTON_MENU, payload=cb.MENU_HOME)]],
        )
        return

    slot = session.data.get("slot")
    if not isinstance(slot, dict):
        await session.finish()
        await ctx.answer(
            event,
            text.slot_taken(),
            [[Btn("Выбрать время заново", payload=cb.subbot_open(subbot.id))]],
        )
        return

    values = _values_of(session)

    # Пока клиент набирал телефон, обязательное поле могло остаться пустым
    # или могло исчезнуть из формы. Возвращаемся к недостающему полю.
    missing = ctx.booking.missing_required(subbot, values)
    if missing:
        session.data["values"] = values
        session.data["order"] = [field.id for field in subbot.form_fields]
        await session.set_state(S_CLIENT_FIELD, pending_field=missing[0].id)
        await _ask_field(ctx, event, missing[0])
        return

    try:
        booking = await ctx.booking.create_booking(
            subbot=subbot,
            user_id=user_id,
            starts_at=int(slot["starts_at"]),
            ends_at=int(slot["ends_at"]),
            values=values,
        )
    except SlotUnavailableError:
        await session.finish()
        await ctx.answer(
            event,
            text.slot_taken(),
            [[Btn("Выбрать другое время", payload=cb.subbot_open(subbot.id))]],
        )
        return

    await session.finish()
    body = text.booking_created(subbot, booking, text.format_values(subbot, values))
    rows: list[Row] = [
        [Btn(text.BUTTON_MINE, payload=cb.MENU_MINE)],
        [Btn(text.BUTTON_MENU, payload=cb.MENU_HOME)],
    ]
    await ctx.answer(event, body, rows)
    await ctx.after_booking_created(subbot, booking)


# --- Экраны -----------------------------------------------------------------


async def _show_days(ctx: AppContext, event: Any, subbot: Subbot) -> None:
    """Экран выбора дня: кнопка на день со счётчиком свободных слотов."""
    days = await ctx.booking.available_days(subbot, user_id_of(event) or 0)
    if not days:
        await ctx.answer(event, text.all_busy(), [[Btn(text.BUTTON_MENU, payload=cb.MENU_HOME)]])
        return

    today = local_date(_now(), subbot.tz)
    buttons: list[Btn] = [
        Btn(
            f"{format_date_ru(item.day)} · {item.free}",
            payload=cb.day_pick(subbot.id, (item.day - today).days),
        )
        for item in days
    ]

    rows = wrap(buttons, DAYS_PER_ROW)
    rows.append([Btn(text.BUTTON_MENU, payload=cb.MENU_HOME)])
    await ctx.answer(event, text.pick_day(subbot), rows)


async def _show_slots(ctx: AppContext, event: Any, subbot: Subbot, offset: int) -> None:
    """Экран выбора времени: занятые слоты видны, но не выбираются."""
    day = day_by_offset(subbot, offset)
    slots = await ctx.booking.day_slots(subbot, day)
    now = _now()

    if not any(not slot.taken and slot.starts_at > now for slot in slots):
        await ctx.answer(event, text.no_slots(), _day_nav_rows(subbot, offset))
        return

    tz = subbot.tz
    buttons: list[Btn] = []
    for slot in slots:
        local = datetime.fromtimestamp(slot.starts_at, tz=tz)
        if slot.taken or slot.starts_at <= now:
            buttons.append(Btn(f"{TAKEN_PREFIX}{local:%H:%M}", payload=cb.slot_busy(slot.starts_at)))
        else:
            buttons.append(
                Btn(f"{local:%H:%M}", payload=cb.slot_pick(subbot.id, offset, local.hour * 60 + local.minute))
            )

    taken = sum(1 for slot in slots if slot.taken)
    rows = wrap(buttons, SLOTS_PER_ROW)
    rows.append(_day_nav_rows(subbot, offset)[0])

    header = text.pick_slot(subbot, day)
    if taken:
        header += f"\n\nЗанято: {taken}. Слоты со знаком «{TAKEN_PREFIX.strip()}» уже недоступны."
    await ctx.answer(event, header, rows)


def _within_horizon(subbot: Subbot, offset: int) -> bool:
    """Входит ли день в окно записи.

    Проверяется на каждое нажатие, а не только при показе списка дней:
    экран со слотами мог остаться открытым, пока мастер уменьшил горизонт.
    """
    return 0 <= offset < subbot.horizon_days


def _day_nav_rows(subbot: Subbot, offset: int) -> list[Row]:
    """Навигация по дням: назад, в меню, вперёд.

    На первом дне «назад» возвращает к списку дней, а не к тому же дню:
    иначе кнопка ничего не делала бы и выглядела бы сломанной. Вперёд на
    последнем дне горизонта кнопки нет — за её пределами записи нет.
    """
    back = (
        Btn(text.BUTTON_BACK, payload=cb.subbot_open(subbot.id))
        if offset == 0
        else Btn("⬅️", payload=cb.day_pick(subbot.id, offset - 1))
    )
    middle = [Btn(text.BUTTON_MENU, payload=cb.MENU_HOME)]
    forward = (
        [Btn("➡️", payload=cb.day_pick(subbot.id, offset + 1))] if offset + 1 < subbot.horizon_days else []
    )
    return [[back, *middle, *forward]]


async def _show_confirm(ctx: AppContext, event: Any, subbot: Subbot, session: Session) -> None:
    """Экран подтверждения: время, введённые данные и кнопка записи."""
    values = _values_of(session)
    slot = session.data.get("slot") or {}

    tz = subbot.tz
    start_local = datetime.fromtimestamp(int(slot.get("starts_at", 0)), tz=tz)
    end_local = datetime.fromtimestamp(int(slot.get("ends_at", 0)), tz=tz)
    day = _day_of(session, start_local.date())

    body = text.confirm_form(subbot, f"{format_date_ru(day)}\n{start_local:%H:%M}–{end_local:%H:%M}")
    if values:
        body += text.format_value_lines(subbot, values)

    rows: list[Row] = [
        [Btn("✅ Записаться", payload=cb.BOOKING_CONFIRM)],
        [
            Btn("Другое время", payload=cb.subbot_open(subbot.id)),
            Btn(text.BUTTON_CANCEL, payload=cb.MENU_BOOK),
        ],
    ]
    await ctx.answer(event, body, rows)


# --- Ввод формы ------------------------------------------------------------


async def _start_form(
    ctx: AppContext,
    event: Any,
    subbot: Subbot,
    session: Session,
    day: date,
    slot: Slot,
) -> None:
    """Сохраняет выбор слота и начинает ввод полей формы."""
    session.data["values"] = _values_of(session)
    await session.set_state(
        S_CLIENT_FIELD,
        subbot_id=subbot.id,
        day=day.isoformat(),
        slot={"starts_at": slot.starts_at, "ends_at": slot.ends_at},
        order=[field.id for field in subbot.form_fields],
    )

    if not subbot.form_fields:
        await _show_confirm(ctx, event, subbot, session)
        return

    await _advance(ctx, event, subbot, session)


async def _advance(ctx: AppContext, event: Any, subbot: Subbot, session: Session) -> None:
    """Переходит к следующему незаполненному полю или к подтверждению."""
    order = [int(item) for item in (session.data.get("order") or [])]
    values = _values_of(session)

    while order:
        field = _find_field(subbot, order[0])
        if field is None or values.get(field.id):
            order.pop(0)
            continue

        # Необязательные поля пропускаем, но если клиент уже начал ввод
        # именно это поле (pending_field) — спрашиваем до конца.
        if field.is_required or session.data.get("pending_field") == field.id:
            session.data["order"] = order
            session.data["pending_field"] = field.id
            await session.set_state(S_CLIENT_FIELD)
            await _ask_field(ctx, event, field)
            return

        order.pop(0)

    # Все поля собраны. pending_field обязательно сбрасывается: пока он
    # указывает на уже отвеченное поле, случайный текст на экране
    # подтверждения сочтётся ответом и вызовет ошибку «поле обязательное».
    await session.set_state(S_CLIENT_FIELD, order=[], pending_field=None, buffer=None)
    await _show_confirm(ctx, event, subbot, session)


async def _ask_field(ctx: AppContext, event: Any, field: FormField) -> None:
    """Просит значение одного поля."""
    rows: list[Row] = []
    if is_comment_field(field.field_type):
        rows.append([Btn(text.BUTTON_DONE, payload=cb.field_form_finish())])
    rows.append([Btn(text.BUTTON_CANCEL, payload=cb.MENU_BOOK)])
    await ctx.answer(event, text.ask_field(field.label, field.is_required, field.max_length), rows)


async def _consume_field(ctx: AppContext, event: Any, subbot: Subbot, session: Session) -> None:
    """Обрабатывает одно введённое значение поля."""
    field = _find_field(subbot, int(session.data.get("pending_field") or 0))
    if field is None:
        # Диалог ждёт нажатия «Записаться», а не текста. Сессию не
        # трогаем: она ещё нужна подтверждению.
        return

    raw = message_text(event).strip()

    if is_comment_field(field.field_type) and raw:
        await _append_multiline(ctx, event, subbot, session, field, raw)
        return

    await session.update(buffer=raw)
    if not await _commit_pending(ctx, event, subbot, session):
        return
    await _advance(ctx, event, subbot, session)


async def _commit_pending(ctx: AppContext, event: Any, subbot: Subbot, session: Session) -> bool:
    """Записывает ответ текущего поля из буфера и убирает его из очереди.

    Общая точка для обычного поля и для многострочного комментария: в
    обоих случаях значение лежит в одном и том же буфере.

    Возвращает False, если ответ не прошёл проверку — тогда вопрос
    остаётся на экране.
    """
    field = _find_field(subbot, int(session.data.get("pending_field") or 0))
    if field is None:
        return True

    raw = str(session.data.get("buffer") or "")
    value, error = validate_client_field(ctx, subbot, field, raw)
    if error:
        rows: list[Row] = []
        if is_comment_field(field.field_type):
            rows.append([Btn(text.BUTTON_DONE, payload=cb.field_form_finish())])
        rows.append([Btn(text.BUTTON_CANCEL, payload=cb.MENU_BOOK)])
        ask = text.ask_field(field.label, field.is_required, field.max_length)
        await ctx.answer(event, f"{error}\n\n{ask}", rows)
        return False

    values = _values_of(session)
    if value:
        values[field.id] = value
    else:
        # Пустой ответ на необязательное поле — считаем его пропущенным.
        values.pop(field.id, None)

    order = [int(item) for item in (session.data.get("order") or [])]
    if field.id in order:
        order.remove(field.id)

    await session.set_state(
        S_CLIENT_FIELD,
        values=values,
        order=order,
        pending_field=None,
        buffer=None,
    )
    return True


async def _append_multiline(
    ctx: AppContext, event: Any, subbot: Subbot, session: Session, field: FormField, raw: str
) -> None:
    """Копит многострочный ответ, пока клиент не нажмёт «Готово»."""
    buffer = str(session.data.get("buffer") or "")
    merged = f"{buffer}\n{raw}" if buffer else raw
    limit = min(field.max_length or 255, 500)

    if len(merged) > limit:
        await ctx.answer(
            event,
            f"Уже {len(merged)} символов, а можно {limit}. Закончите мысль и нажмите «{text.BUTTON_DONE}».",
            [[Btn(text.BUTTON_DONE, payload=cb.field_form_finish())]],
        )
        return

    await session.set_state(S_CLIENT_COMMENT, buffer=merged)
    await ctx.answer(
        event,
        f"Сохранено ({len(merged)} символов). Можно дописать или нажать «{text.BUTTON_DONE}».",
        [[Btn(text.BUTTON_DONE, payload=cb.field_form_finish())]],
    )


# --- Вспомогательное --------------------------------------------------------


async def _require_subbot(ctx: AppContext, subbot_id: int) -> Subbot | None:
    """Подбот, готовый принимать записи: существует, активен и настроен."""
    subbot = await ctx.repos.subbots.get_by_id(subbot_id)
    if subbot is None or not subbot.is_active or not subbot.is_configured:
        return None
    return subbot


def _find_field(subbot: Subbot, field_id: int) -> FormField | None:
    for field in subbot.form_fields:
        if field.id == field_id:
            return field
    return None


def _values_of(session: Session) -> dict[int, str]:
    """Значения формы из сессии с приведёнными ключами-идентификаторами."""
    raw = session.data.get("values") or {}
    return {int(key): str(value) for key, value in raw.items()}


def _day_of(session: Session, fallback: date) -> date:
    stored = session.data.get("day")
    if not stored:
        return fallback
    try:
        return datetime.fromisoformat(stored).date()
    except ValueError:
        return fallback


def _arg_int(event: MessageCallback, index: int) -> int | None:
    parts = cb.parse_callback(callback_payload(event))
    return cb.to_int(parts[index]) if len(parts) > index else None


def _now() -> int:
    return int(time.time())


__all__ = ["DAYS_PER_ROW", "SLOTS_PER_ROW", "handle_form_input", "register_client_booking"]
