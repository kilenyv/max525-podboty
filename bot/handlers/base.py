"""Общий контекст приложения и хелперы для обработчиков.

Прикладной код получает сюда сервисы и общие операции (ответить,
показать меню, разобрать состояние сессии), а не собирает их заново.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from bot.config import Settings
from bot.db.connection import Database
from bot.db.repositories import Repositories
from bot.db.repositories.bookings import SlotUnavailableError
from bot.db.repositories.sessions import Session
from bot.domain import callback as cb
from bot.domain.enums import CancelSource, FieldType
from bot.domain.models import Booking, Subbot
from bot.logging_config import get_logger
from bot.services import text
from bot.services.booking import BookingService, FieldValidationError
from bot.services.max_sender import MaxSender
from bot.services.notifier import Notifier
from bot.services.scheduler import Scheduler, schedule_reminder
from bot.services.ui import Btn, Row

log = get_logger(__name__)

#: Значения состояний сессии. Один источник истины для FSM.
S_CREATE_TITLE = "adm:create:title"
S_CREATE_DESC = "adm:create:desc"
S_SCHEDULE_RANGE = "adm:sch:range"
S_SCHEDULE_BREAK = "adm:sch:break"
S_SCHEDULE_NUMBER = "adm:sch:number"
S_RULES_TZ = "adm:rul:tz"
S_CLIENT_FIELD = "cli:field"
S_CLIENT_COMMENT = "cli:comment"

#: Все состояния, в которых бот ждёт свободный текст.
TEXT_STATES = frozenset(
    {
        S_CREATE_TITLE,
        S_CREATE_DESC,
        S_SCHEDULE_RANGE,
        S_SCHEDULE_BREAK,
        S_SCHEDULE_NUMBER,
        S_RULES_TZ,
        S_CLIENT_FIELD,
        S_CLIENT_COMMENT,
    }
)

#: Сколько подботов показывать в запасном списке «Записаться».
BOOKING_LIST_LIMIT = 20


@dataclass
class AppContext:
    """Всё, чем пользуются обработчики. Создаётся один раз в app.py."""

    settings: Settings
    db: Database
    repos: Repositories
    sender: MaxSender
    booking: BookingService
    notifier: Notifier
    scheduler: Scheduler

    # --- Ответы -----------------------------------------------------------

    async def answer(self, event: Any, body: str, rows: list[Row] | None = None, **kwargs: Any) -> bool:
        """Отвечает в чат, откуда пришло событие."""
        chat_id = chat_id_of(event)
        if chat_id is None:
            return False
        return await self.sender.send(chat_id, body, rows, **kwargs)

    async def notify(self, user_id: int, body: str, rows: list[Row] | None = None) -> bool:
        return await self.sender.send(user_id, body, rows)

    # --- Доступ -----------------------------------------------------------

    # --- Сессии -----------------------------------------------------------

    async def session(self, user_id: int) -> Session:
        return await self.repos.sessions.load(user_id)

    # --- Корневые меню ----------------------------------------------------

    async def show_main_menu(self, event: Any) -> None:
        rows: list[Row] = [
            [Btn(text.BUTTON_BOOK, payload=cb.MENU_BOOK)],
            [Btn(text.BUTTON_MINE, payload=cb.MENU_MINE)],
            [Btn(text.BUTTON_PANEL, payload=cb.MENU_PANEL)],
            [Btn(text.BUTTON_HELP, payload=cb.MENU_HELP)],
        ]
        await self.answer(event, text.WELCOME, rows)

    async def show_help(self, event: Any) -> None:
        await self.answer(event, text.HELP, [[Btn(text.BUTTON_MENU, payload=cb.MENU_HOME)]])

    async def show_booking_start(self, event: Any) -> None:
        """Список подботов, куда можно записаться.

        Обычно клиент приходит по прямой ссылке подбота и сюда не попадает.
        Этот экран — запасной путь для тех, кто открыл бота без ссылки.
        """
        rows: list[Row] = []
        for subbot_id in await self.repos.subbots.list_active_ids():
            subbot = await self.repos.subbots.get_by_id(subbot_id)
            if subbot is None or not subbot.is_configured:
                continue
            rows.append([Btn(f"✂️ {subbot.title}", payload=cb.subbot_open(subbot.id))])

        if not rows:
            await self.answer(
                event,
                text.no_subbots_yet(),
                [[Btn(text.BUTTON_PANEL, payload=cb.MENU_PANEL)]],
            )
            return

        header = "К кому хотите записаться?"
        if len(rows) > BOOKING_LIST_LIMIT:
            rows = rows[:BOOKING_LIST_LIMIT]
            header += f"\n\nПоказаны первые {BOOKING_LIST_LIMIT}. Лучше приходить по прямой ссылке мастера."

        rows.append([Btn(text.BUTTON_MENU, payload=cb.MENU_HOME)])
        await self.answer(event, header, rows)

    async def show_my_bookings(self, event: Any) -> None:
        user_id = user_id_of(event)
        if user_id is None:
            return

        bookings = await self.repos.bookings.list_user_upcoming(user_id)
        if not bookings:
            await self.answer(
                event,
                text.my_bookings_header(0),
                [
                    [Btn(text.BUTTON_BOOK, payload=cb.MENU_BOOK)],
                    [Btn(text.BUTTON_MENU, payload=cb.MENU_HOME)],
                ],
            )
            return

        rows: list[Row] = []
        for booking in bookings:
            subbot = await self.repos.subbots.get_by_id(booking.subbot_id)
            title = subbot.title if subbot else "Запись"
            label = text.my_booking_line(title, booking)
            rows.append([Btn(f"❌ {label}", payload=cb.booking_cancel(booking.id))])

        rows.append([Btn(text.BUTTON_MENU, payload=cb.MENU_HOME)])
        await self.answer(event, text.my_bookings_header(len(bookings)), rows)

    # --- Запись ------------------------------------------------------------

    async def after_booking_created(self, subbot: Subbot, booking: Booking) -> None:
        """Уведомить обе стороны и поставить напоминание."""
        await self.notifier.notify_client_confirmed(subbot, booking)
        await self.notifier.notify_owner_of_booking(subbot, booking)
        await schedule_reminder(self.repos, self.notifier, subbot, booking)

    async def cancel_booking(self, subbot: Subbot, booking: Booking, source: CancelSource) -> bool:
        if source is CancelSource.CLIENT:
            try:
                cancelled = await self.booking.cancel_by_client(subbot, booking)
            except PermissionError as exc:
                hours = int(str(exc).split(":", 1)[-1])
                await self.notify(booking.user_id, text.cancel_too_late(hours), [])
                return False
        else:
            cancelled = await self.booking.cancel_by_owner(booking)

        if cancelled is None:
            return False
        await self.notifier.handle_cancel(subbot, cancelled, source)
        return True


# --- Доступ ----------------------------------------------------------------


async def require_owned_subbot(ctx: AppContext, subbot_id: int, user_id: int) -> Subbot | None:
    """Подбот, только если он принадлежит пользователю.

    Единственный барьер между админкой и чужими настройками: регистрации нет,
    поэтому проверка владения выполняется на каждом действии.
    """
    subbot = await ctx.repos.subbots.get_owned(subbot_id, user_id)
    if subbot is None:
        log.warning("Пользователь %s запросил чужой подбот %s", user_id, subbot_id)
    return subbot


# --- Валидация ввода клиента -----------------------------------------------

FIELD_ERRORS = {
    "EMPTY": text.EMPTY_REQUIRED,
    "PHONE": text.INVALID_PHONE,
    "TOO_LONG": text.TOO_LONG,
}


def field_error_message(error: str) -> str:
    return FIELD_ERRORS.get(error, "Проверьте значение и попробуйте ещё раз.")


def validate_client_field(ctx: AppContext, subbot: Subbot, field, raw: str) -> tuple[str, str | None]:  # noqa: ANN001
    """Возвращает (значение, текст_ошибки)."""
    try:
        return ctx.booking.validate_field(subbot, field, raw), None
    except FieldValidationError as exc:
        return "", field_error_message(str(exc))


# --- Разбор callback-кодов -------------------------------------------------


def callback_payload(event: Any) -> str:
    """Достаёт payload из объекта обновления о нажатии кнопки."""
    callback = getattr(event, "callback", None)
    if callback is None:
        return ""
    return str(getattr(callback, "payload", None) or "")


def message_text(event: Any) -> str:
    """Текст входящего сообщения: сначала body.text, потом запасное поле."""
    message = getattr(event, "message", None)
    if message is None:
        return ""
    body = getattr(message, "body", None)
    value = getattr(body, "text", None) if body is not None else None
    if value:
        return str(value)
    return str(getattr(message, "text", "") or "")


def wrap(buttons: list[Btn], per_row: int) -> list[Row]:
    """Раскладывает кнопки в ряды — длинные списки иначе нечитаемы."""
    return [
        row for row in (buttons[index : index + per_row] for index in range(0, len(buttons), per_row)) if row
    ]


def is_comment_field(field_type: FieldType) -> bool:
    """Комментарий и доп. текст вводят в несколько строк — заканчивается кнопкой."""
    return field_type in (FieldType.COMMENT, FieldType.TEXT)


# --- Прочее -----------------------------------------------------------------


def now_ts() -> int:
    return int(time.time())


def chat_id_of(event: Any) -> int | None:
    """Идентификатор чата из любого типа обновления.

    Порядок важен: у BotStarted поле chat_id лежит на самом событии,
    у остальных — внутри message.recipient или chat.
    """
    chat_id = getattr(event, "chat_id", None)
    if isinstance(chat_id, int):
        return chat_id

    chat = getattr(event, "chat", None)
    if chat is not None and getattr(chat, "chat_id", None) is not None:
        return chat.chat_id

    message = getattr(event, "message", None)
    recipient = getattr(message, "recipient", None) if message is not None else None
    chat_id = getattr(recipient, "chat_id", None) if recipient is not None else None
    return chat_id if isinstance(chat_id, int) else None


def user_of(event: Any) -> Any:
    """Объект пользователя из любого типа обновления.

    У MessageCreated пользователь лежит в message.sender, у BotStarted —
    в user, у MessageCallback — в callback.user. Имена атрибутов у maxapi
    разные, поэтому ищем по всем сразу.
    """
    for holder in (event, getattr(event, "callback", None), getattr(event, "message", None)):
        if holder is None:
            continue
        user = (
            getattr(holder, "user", None)
            or getattr(holder, "from_user", None)
            or getattr(holder, "sender", None)
        )
        if user is not None:
            return user
    return None


def user_id_of(event: Any) -> int | None:
    """Идентификатор пользователя или None, если событие не от пользователя."""
    return getattr(user_of(event), "user_id", None)


__all__ = [
    "S_CLIENT_COMMENT",
    "S_CLIENT_FIELD",
    "S_CREATE_DESC",
    "S_CREATE_TITLE",
    "S_RULES_TZ",
    "S_SCHEDULE_BREAK",
    "S_SCHEDULE_NUMBER",
    "S_SCHEDULE_RANGE",
    "TEXT_STATES",
    "AppContext",
    "SlotUnavailableError",
    "callback_payload",
    "chat_id_of",
    "field_error_message",
    "is_comment_field",
    "message_text",
    "now_ts",
    "require_owned_subbot",
    "user_id_of",
    "user_of",
    "validate_client_field",
    "wrap",
]
