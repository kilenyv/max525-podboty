"""Тексты интерфейса и форматирование.

Собраны в одном месте, чтобы не искать по коду. Интерфейс русский,
время в формате 24 часа.
"""

from __future__ import annotations

from datetime import datetime

from bot.domain.models import Booking, Slot, Subbot
from bot.domain.timeutil import format_date_ru, minutes_to_hhmm

# --- Старт -----------------------------------------------------------------

WELCOME = (
    "👋 <b>Бот записи</b>\n\n"
    "Здесь можно записаться к мастеру или настроить свою страницу записи "
    "и рассылать клиентам ссылку.\n\n"
    "Выберите, что нужно:"
)

BUTTON_BOOK = "✍️ Записаться"
BUTTON_MINE = "📋 Мои записи"
BUTTON_PANEL = "⚙️ Админ-панель"
BUTTON_HELP = "❓ Помощь"
BUTTON_BACK = "⬅️ Назад"
BUTTON_CANCEL = "❌ Отмена"
BUTTON_DONE = "✅ Готово"
BUTTON_MENU = "🏠 Меню"

HELP = (
    "<b>Как это работает</b>\n\n"
    "<b>Записаться</b> — выберите мастера, день и свободное время, "
    "оставьте контакты. Запись подтверждается сразу.\n\n"
    "<b>Админ-панель</b> — создайте свою страницу записи: рабочее время, "
    "перерывы, длительность встречи и поля, которые заполняет клиент. "
    "Бот выдаст ссылку — её можно отправлять кому угодно.\n\n"
    "Отменить запись можно в разделе «Мои записи»."
)

NOT_A_SUBBOT = "Ссылка не найдена. Проверьте адрес — возможно, страницу удалили или в ссылке опечатка."

# --- Клиент: выбор ---------------------------------------------------------


def no_subbots_yet() -> str:
    return (
        "Пока записаться не к кому: мастера ещё не добавили.\n\n"
        "Если вы принимаете клиентов сами — загляните в "
        "<b>Админ-панель</b> и создайте свою страницу записи."
    )


def subbot_card(subbot: Subbot) -> str:
    lines = [f"<b>{subbot.title}</b>"]
    if subbot.description:
        lines.append(subbot.description)

    tz_label = subbot.timezone.replace("_", " ")
    lines.append("")
    lines.append(f"🕐 Часовой пояс: {tz_label}")
    lines.append(f"⏱ Длительность: {format_duration(subbot.slot_minutes)}")
    lines.append(f"📅 Запись открыта на {subbot.horizon_days} дн. вперёд")

    if not subbot.is_configured:
        lines.append("")
        lines.append("⚠️ Мастер ещё не заполнил расписание.")

    return "\n".join(lines)


def pick_day(subbot: Subbot) -> str:
    return f"<b>{subbot.title}</b>\n\nВыберите день. Свободные слоты показаны числом."


def pick_slot(subbot: Subbot, day) -> str:  # noqa: ANN001 - date
    return (
        f"<b>{subbot.title}</b>\n"
        f"{format_date_ru(day)}\n\n"
        "Свободное время можно выбрать ниже. Занятые слоты отмечены точкой."
    )


def confirm_form(subbot: Subbot, booking_text: str) -> str:
    return f"<b>{subbot.title}</b>\n{booking_text}\n\nПроверьте данные и подтвердите запись."


def booking_created(subbot: Subbot, booking: Booking, values_text: str) -> str:
    return (
        f"✅ <b>Вы записаны</b>\n\n"
        f"{subbot.title}\n{format_date_ru(booking.start_local.date())}, "
        f"{booking.start_local:%H:%M}–{booking.end_local:%H:%M}\n"
        f"{values_text}\n\n"
        "Отменить запись можно в разделе «Мои записи»."
    )


def booking_cancelled_client(subbot_title: str, booking: Booking, values_text: str = "") -> str:
    tail = f"\n{values_text}" if values_text else ""
    return (
        f"🚫 Запись отменена\n\n"
        f"{subbot_title}\n"
        f"{format_date_ru(booking.start_local.date())}, "
        f"{booking.start_local:%H:%M}–{booking.end_local:%H:%M}{tail}"
    )


def booking_cancelled_owner(subbot_title: str, booking: Booking, values_text: str) -> str:
    return (
        f"🚫 <b>Запись отменена</b>\n\n"
        f"{subbot_title}\n"
        f"{format_date_ru(booking.start_local.date())}, "
        f"{booking.start_local:%H:%M}–{booking.end_local:%H:%M}\n{values_text}"
    )


def slot_taken() -> str:
    return "⏰ Это время только что заняли. Выберите другое."


def no_slots() -> str:
    return "На выбранный день свободного времени нет. Выберите другой день или загляните позже."


def all_busy() -> str:
    return (
        "К сожалению, в ближайшие дни всё занято. Попробуйте заглянуть позже — слоты появляются после отмен."
    )


def out_of_horizon(subbot: Subbot) -> str:
    return (
        f"⛔ Запись открыта на {subbot.horizon_days} дн. "
        f"вперёд — этот день уже за пределами окна. "
        f"Выберите ближайший свободный день."
    )


# --- Клиент: свои записи ---------------------------------------------------


def my_bookings_header(count: int) -> str:
    if count:
        return f"📋 <b>Ваши записи</b> — предстоит {count}"
    return "📋 <b>У вас нет активных записей</b>"


def my_booking_line(subbot_title: str, booking: Booking) -> str:
    return (
        f"{subbot_title} — {format_date_ru(booking.start_local.date())}, "
        f"{booking.start_local:%H:%M}–{booking.end_local:%H:%M}"
    )


def cancel_too_late(hours: int) -> str:
    return f"Отменить запись можно не позднее, чем за {hours} ч. Свяжитесь с мастером напрямую."


def cancel_confirm(subbot_title: str, booking: Booking) -> str:
    return (
        f"Отменить запись?\n\n"
        f"{subbot_title}\n"
        f"{format_date_ru(booking.start_local.date())}, "
        f"{booking.start_local:%H:%M}–{booking.end_local:%H:%M}"
    )


# --- Админ-панель ----------------------------------------------------------

PANEL_ROOT = (
    "⚙️ <b>Админ-панель</b>\n\n"
    "Здесь ваши страницы записи. Клиенты заходят по ссылке и видят "
    "только форму записи — настройки им недоступны."
)


def panel_empty() -> str:
    return (
        "⚙️ <b>Админ-панель</b>\n\n"
        "У вас пока нет ни одной страницы записи.\n\n"
        "Создайте первую — бот настроит рабочее время, а ссылку для "
        "рассылки клиентам выдаст сразу."
    )


def subbot_admin_card(subbot: Subbot, link: str, active_bookings: int) -> str:
    tz_label = subbot.timezone.replace("_", " ")
    status = "🟢 готов" if subbot.is_configured else "🔴 не настроен"
    return (
        f"<b>{subbot.title}</b>  ({status})\n"
        f"🔗 <code>{link}</code>\n"
        f"🕐 {tz_label} · ⏱ {format_duration(subbot.slot_minutes)} · "
        f"📅 {subbot.horizon_days} дн.\n"
        f"📋 активных записей: {active_bookings}"
    )


def schedule_day_header(weekday_name: str, is_working: bool) -> str:
    state = "рабочий день" if is_working else "нерабочий"
    return f"<b>{weekday_name}</b> — {state}"


def schedule_day_view(weekday_name: str, intervals, breaks) -> str:  # noqa: ANN001
    if not intervals:
        body = "Рабочее время не задано."
    else:
        body = "Рабочее время: " + ", ".join(str(item) for item in intervals)

    if breaks:
        body += "\nПерерывы: " + ", ".join(str(item) for item in breaks)
    else:
        body += "\nПерерывов нет"

    return f"<b>{weekday_name}</b>\n{body}"


def fields_view(subbot: Subbot) -> str:
    if not subbot.form_fields:
        return "<b>Поля формы</b>\n\nПолей нет — клиент ничего не вводит."
    lines = ["<b>Поля формы</b>", ""]
    for item in subbot.form_fields:
        mark = "обязательное" if item.is_required else "необязательное"
        lines.append(f"• {item.label} — {mark}")
    return "\n".join(lines)


def rules_view(subbot: Subbot) -> str:
    reminder = f"за {subbot.reminder_hours} ч" if subbot.reminder_hours > 0 else "выключены"
    digest = f"в {subbot.digest_hour:02d}:00" if subbot.digest_hour >= 0 else "выключен"
    return (
        f"<b>Правила записи</b>\n\n"
        f"🕐 Часовой пояс: {subbot.timezone.replace('_', ' ')}\n"
        f"⏱ Длительность встречи: {format_duration(subbot.slot_minutes)}\n"
        f"📅 Горизонт записи: {subbot.horizon_days} дн. вперёд\n"
        f"⏳ Отмена не позднее чем за: {subbot.cancel_notice_hours} ч\n"
        f"🔔 Напоминание клиенту: {reminder}\n"
        f"📆 Дайджест владельцу: {digest}"
    )


def booking_card(subbot_title: str, booking: Booking, values_text: str) -> str:
    return (
        f"🆕 <b>Новая запись</b>\n\n"
        f"<b>{subbot_title}</b>\n"
        f"{format_date_ru(booking.start_local.date())}, "
        f"{booking.start_local:%H:%M}–{booking.end_local:%H:%M}\n\n"
        f"{values_text}"
    )


def digest_header(subbot_title: str, count: int) -> str:
    return f"📆 <b>Записи на сегодня: {subbot_title}</b> ({count})"


def digest_empty(subbot_title: str) -> str:
    return f"📆 На сегодня записей нет: {subbot_title}"


def reminder_client(subbot_title: str, booking: Booking) -> str:
    return (
        f"🔔 <b>Напоминание о записи</b>\n\n"
        f"{subbot_title}\n"
        f"{format_date_ru(booking.start_local.date())}, "
        f"{booking.start_local:%H:%M}–{booking.end_local:%H:%M}\n\n"
        "Отменить, если не получится, можно в разделе «Мои записи»."
    )


# --- Ввод данных -----------------------------------------------------------


def ask_field(field_label: str, required: bool, max_length: int) -> str:
    hint = " — обязательное поле" if required else " — необязательное, можно пропустить"
    extra = f" (до {max_length} символов)" if max_length > 0 else ""
    return f"{field_label}{hint}{extra}\n\nПришлите значение сообщением:"


INVALID_PHONE = "Не похоже на номер телефона. Пример: +7 999 123-45-67"
TOO_LONG = "Слишком длинное значение, сократите."
EMPTY_REQUIRED = "Это поле обязательное."


def ask_time_range(what: str) -> str:
    return f"{what}\n\nПришлите время в формате <code>10:00-18:00</code>:"


def ask_number(what: str, current: int | None = None) -> str:
    tail = f"\n\nСейчас: {current}." if current is not None else ""
    return f"{what}{tail}\n\nПришлите число:"


# --- Форматирование --------------------------------------------------------


def format_duration(minutes: int) -> str:
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} ч" if hours != 1 else "1 час"
    if minutes < 60:
        return f"{minutes} мин"
    return f"{minutes // 60} ч {minutes % 60} мин"


def tz_label(name: str) -> str:
    """«Europe/Kaliningrad» → «Europe / Kaliningrad»: читаемее в одну строку."""
    return name.replace("_", " ")


def format_slot_label(slot: Slot, tz) -> str:  # noqa: ANN001 - ZoneInfo
    start = datetime.fromtimestamp(slot.starts_at, tz=tz)
    end = datetime.fromtimestamp(slot.ends_at, tz=tz)
    return f"{start:%H:%M}–{end:%H:%M}"


def format_slot_short(slot: Slot, tz) -> str:  # noqa: ANN001
    start = datetime.fromtimestamp(slot.starts_at, tz=tz)
    return f"{start:%H:%M}"


def format_booking_line(subbot_title: str, booking: Booking) -> str:
    return (
        f"{subbot_title} — {format_date_ru(booking.start_local.date())}, "
        f"{booking.start_local:%H:%M}–{booking.end_local:%H:%M}"
    )


def format_values(subbot: Subbot, values: dict[int, str]) -> str:
    if not values:
        return ""
    parts = []
    for item in subbot.form_fields:
        value = values.get(item.id)
        if value:
            parts.append(f"{item.label}: {value}")
    return "\n".join(parts) if parts else ""


def format_value_lines(subbot: Subbot, values: dict[int, str]) -> str:
    text = format_values(subbot, values)
    return f"\n{text}" if text else ""


def format_minutes_range(start: int, end: int) -> str:
    return f"{minutes_to_hhmm(start)}–{minutes_to_hhmm(end)}"
