"""Общие приспособления для тестов: настоящая БД, фейковый MAX, реальные события.

Сценарии проверяются через настоящий `Dispatcher.handle`, а не прямым
вызовом обработчиков: так тест ловит и ошибки разбора callback-кодов, и
ошибки порядка регистрации — ровно то, что ломается в бою.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import pytest_asyncio
from maxapi.types import BotStarted, MessageCallback, MessageCreated, User
from maxapi.types.callback import Callback
from maxapi.types.chats import Chat, ChatStatus, ChatType
from maxapi.types.message import Message, MessageBody, Recipient

from bot.config import Settings
from bot.db.connection import Database
from bot.db.repositories import Repositories
from bot.domain import callback as cb
from bot.domain.models import Interval
from bot.domain.timeutil import hhmm_to_minutes
from bot.handlers import register_all
from bot.handlers.base import AppContext
from bot.services.booking import BookingService
from bot.services.max_sender import MaxSender
from bot.services.notifier import Notifier
from bot.services.scheduler import Scheduler
from bot.services.ui import Btn, Row

OWNER_ID = 1001
CLIENT_ID = 2002
STRANGER_ID = 3003


# --- Фейковый MAX -----------------------------------------------------------


@dataclass
class SentMessage:
    chat_id: int
    text: str
    rows: list[Row]

    def payloads(self) -> list[str]:
        return [btn.payload for row in self.rows for btn in row if btn.payload]

    def buttons(self) -> list[Btn]:
        return [btn for row in self.rows for btn in row]

    def has_text(self, needle: str) -> bool:
        return needle.lower() in self.text.lower()

    def has_button(self, label: str) -> bool:
        return any(label in btn.text for btn in self.buttons())


class FakeBot:
    """Записывает отправленные сообщения вместо обращения к API.

    Диспетчер при инициализации спрашивает профиль бота, поэтому `get_me`
    тоже нужен: без него `Dispatcher.startup` не пройдёт.
    """

    auto_check_subscriptions = False

    def __init__(self) -> None:
        self.sent: list[SentMessage] = []
        self.me = User(user_id=1, first_name="TestBot", username="testbot", is_bot=True, last_activity_time=0)
        self.dispatcher = None

    async def get_me(self) -> User:
        return self.me

    async def send_message(self, chat_id, text, attachments=None, parse_mode=None, notify=None):
        self.sent.append(SentMessage(chat_id, text, []))
        return object()


class RecordingSender(MaxSender):
    """MaxSender, который складывает сообщения в список, а не шлёт их."""

    def __init__(self, bot: Any) -> None:
        super().__init__(bot, rate_limit=1000, retries=1)
        self.sent: list[SentMessage] = []
        self.rows: dict[int, list[Row]] = {}
        self.bodies: dict[int, list[str]] = {}

    async def send(
        self,
        chat_id: int,
        text: str,
        rows: list[Row] | None = None,
        *,
        notify: bool | None = None,
    ) -> bool:
        del notify
        rows = rows or []
        self.sent.append(SentMessage(chat_id, text, rows))
        self.rows.setdefault(chat_id, []).extend(rows)
        self.bodies.setdefault(chat_id, []).append(text)
        return True

    # --- Удобные помощники для проверок ---

    def to(self, chat_id: int) -> list[SentMessage]:
        return [message for message in self.sent if message.chat_id == chat_id]

    def last(self, chat_id: int) -> SentMessage:
        messages = self.to(chat_id)
        assert messages, f"в чат {chat_id} ничего не отправлено"
        return messages[-1]

    def payloads_of(self, chat_id: int) -> list[str]:
        return [payload for message in self.to(chat_id) for payload in message.payloads()]

    def has_text(self, chat_id: int, needle: str) -> bool:
        return any(message.has_text(needle) for message in self.to(chat_id))

    def clear(self) -> None:
        self.sent.clear()
        self.rows.clear()
        self.bodies.clear()


# --- Приложение в тесте -----------------------------------------------------


class Harness:
    """Всё вместе: БД, сервисы, диспетчер с настоящими обработчиками."""

    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.repos = Repositories(db)
        self.bot = FakeBot()
        self.sender = RecordingSender(self.bot)
        self.booking = BookingService(db, self.repos)
        self.notifier = Notifier(db, self.repos, self.sender, self.booking)
        self.scheduler = Scheduler(
            db=db,
            repos=self.repos,
            notifier=self.notifier,
            booking_service=self.booking,
            poll_interval=3600,
        )
        self.ctx = AppContext(
            settings=settings,
            db=db,
            repos=self.repos,
            sender=self.sender,
            booking=self.booking,
            notifier=self.notifier,
            scheduler=self.scheduler,
        )
        self.dp = None
        self.clock = int(time.time())

    async def start(self) -> None:
        """Собирает диспетчер и прогревает его, как это делает polling.

        `Dispatcher.startup` строит индекс обработчиков по типу события;
        без него `handle` не найдёт ни одного обработчика.
        """
        from maxapi import Dispatcher

        self.dp = Dispatcher()
        await register_all(self.dp, self.ctx)
        await self.dp.startup(self.bot)

    # --- Отправка событий ---

    async def press(self, user_id: int, payload: str, chat_id: int | None = None) -> SentMessage:
        """Нажатие inline-кнопки."""
        await self._callback(user_id, payload, chat_id or user_id)
        return self.sender.last(chat_id or user_id)

    async def type(self, user_id: int, text: str, chat_id: int | None = None) -> SentMessage:
        """Обычное сообщение пользователя."""
        self.sender.clear()
        await self._text(user_id, text, chat_id or user_id)
        return self.sender.last(chat_id or user_id)

    async def start_bot(
        self, user_id: int, payload: str | None = None, chat_id: int | None = None
    ) -> SentMessage:
        """Нажатие «Начать» или переход по диплинку."""
        self.sender.clear()
        event = BotStarted(
            timestamp=self.clock,
            chat_id=chat_id or user_id,
            user=user_obj(user_id),
            payload=payload,
        )
        await self.dp.handle(event)
        return self.sender.last(chat_id or user_id)

    async def _callback(self, user_id: int, payload: str, chat_id: int) -> None:
        event = MessageCallback(
            timestamp=self.clock,
            from_user=user_obj(user_id),
            chat=chat_obj(chat_id),
            message=message_obj(chat_id, ""),
            callback=Callback(
                timestamp=self.clock,
                callback_id=f"cb{self.clock}",
                payload=payload,
                user=user_obj(user_id),
            ),
        )
        await self.dp.handle(event)

    async def _text(self, user_id: int, text: str, chat_id: int) -> None:
        event = MessageCreated(
            timestamp=self.clock,
            from_user=user_obj(user_id),
            chat=chat_obj(chat_id),
            message=message_obj(chat_id, text),
        )
        await self.dp.handle(event)

    # --- Данные ---

    def today(self):
        """Сегодня в часовом поясе подбота по умолчанию."""
        from zoneinfo import ZoneInfo

        from bot.services.booking import local_date

        return local_date(self.clock, ZoneInfo("Europe/Moscow"))

    def day_at(self, offset: int):
        from datetime import timedelta

        return self.today() + timedelta(days=offset)

    def weekday_today(self) -> int:
        return self.today().isoweekday()

    async def make_bare_subbot(self, owner: int = OWNER_ID, title: str = "Маникюр", **kwargs):
        """Подбот без единого рабочего дня: то, что получает новый мастер.

        Нужен там, где тест сам настраивает расписание, — иначе нечего
        проверять: добавление интервала падало бы как пересечение.
        """
        from bot.domain.enums import FieldType

        wanted = {FieldType(item) for item in kwargs.pop("fields", ("name", "phone"))}

        subbot = await self.repos.subbots.create(
            owner_user_id=owner,
            title=title,
            description=kwargs.pop("description", "Стрижка и маникюр"),
            timezone=kwargs.pop("timezone", "Europe/Moscow"),
        )
        await self.repos.subbots.update_rules(subbot.id, owner, slot_minutes=60)
        for field_type in FieldType:
            await self.repos.subbots.set_field_enabled(
                subbot.id, owner, field_type, enabled=field_type in wanted, is_required=True
            )
        for key, value in kwargs.items():
            await self.repos.subbots.update_rules(subbot.id, owner, **{key: value})
        return await self.repos.subbots.get_by_id(subbot.id)

    async def make_subbot(self, owner: int = OWNER_ID, title: str = "Маникюр", **kwargs):
        """Готовый подбот: 10:00–13:00 все дни недели, слот по часу.

        Длительность слота задаётся явно: по умолчанию в БД 30 минут,
        а проверять надо конкретное расписание, а не значение по умолчанию.
        Поля — `fields=("name", "phone")`; остальные выключаются явно.
        """
        subbot = await self.make_bare_subbot(owner, title, **kwargs)
        for weekday in range(1, 8):
            await self.repos.subbots.add_interval(subbot.id, subbot.owner_user_id, weekday, 600, 780)
        return await self.repos.subbots.get_by_id(subbot.id)

    async def set_interval(self, subbot_id: int, owner: int, weekday: int, start: str, end: str) -> None:
        await self.repos.subbots.add_interval(
            subbot_id, owner, weekday, hhmm_to_minutes(start), hhmm_to_minutes(end)
        )

    async def reload(self, subbot_id: int):
        return await self.repos.subbots.get_by_id(subbot_id)

    # --- Короткие шаги клиентского пути ---

    async def open_days(self, user_id: int, subbot_id: int) -> SentMessage:
        return await self.press(user_id, cb.subbot_open(subbot_id))

    async def pick_first_day(self, user_id: int, subbot_id: int) -> SentMessage:
        screen = await self.open_days(user_id, subbot_id)
        payload = next(p for p in screen.payloads() if p.startswith("d:"))
        return await self.press(user_id, payload)

    async def pick_day(self, user_id: int, subbot_id: int, offset: int) -> SentMessage:
        return await self.press(user_id, cb.day_pick(subbot_id, offset))

    async def pick_first_slot(self, user_id: int, subbot_id: int) -> SentMessage:
        """Выбирает первый свободный слот и попадает на первый вопрос формы."""
        screen = await self.pick_first_day(user_id, subbot_id)
        payload = next(p for p in screen.payloads() if p.startswith("t:"))
        return await self.press(user_id, payload)

    async def pick_slot_on(self, user_id: int, subbot_id: int, offset: int) -> SentMessage:
        """То же, но на выбранном дне: нужно для напоминаний за сутки."""
        screen = await self.pick_day(user_id, subbot_id, offset)
        payload = next(p for p in screen.payloads() if p.startswith("t:"))
        return await self.press(user_id, payload)

    async def book(
        self,
        user_id: int,
        subbot_id: int,
        answers: tuple[str, ...] = ("Анна", "+7 999 123-45-67"),
        offset: int | None = None,
    ) -> SentMessage:
        """Полный цикл: день → слот → ответы на поля → подтверждение."""
        if offset is None:
            await self.pick_first_slot(user_id, subbot_id)
        else:
            await self.pick_slot_on(user_id, subbot_id, offset)
        for answer in answers:
            await self.type(user_id, answer)
        return await self.press(user_id, cb.BOOKING_CONFIRM)


def user_obj(user_id: int) -> User:
    return User(
        user_id=user_id,
        first_name=f"User{user_id}",
        is_bot=False,
        last_activity_time=0,
    )


def chat_obj(chat_id: int) -> Chat:
    return Chat(
        chat_id=chat_id,
        type=ChatType.CHAT,
        status=ChatStatus.ACTIVE,
        title="",
        last_event_time=0,
        participants_count=2,
        is_public=False,
        dialog_with_user=user_obj(chat_id),
    )


def message_obj(chat_id: int, text: str) -> Message:
    return Message(
        recipient=Recipient(user_id=0, chat_id=chat_id, chat_type=ChatType.CHAT),
        timestamp=0,
        body=MessageBody(mid="m1", seq=0, text=text),
    )


# --- Фикстуры ---------------------------------------------------------------


@pytest_asyncio.fixture
async def harness(tmp_path):
    settings = Settings(
        MAX_BOT_TOKEN="test-token",
        BOT_USERNAME="testbot",
        DATABASE_PATH=tmp_path / "test.db",
        LOG_LEVEL="WARNING",
    )
    db = Database(settings.database_path)
    await db.connect()
    app = Harness(db, settings)
    await app.start()
    try:
        yield app
    finally:
        await db.close()


def interval(start: str, end: str) -> Interval:
    return Interval(start=hhmm_to_minutes(start), end=hhmm_to_minutes(end))


@dataclass
class Slots:
    """Кнопки-время на экране выбора слота.

    Отбираются по коду (`t:` — свободный, `z:` — занятый), а не по всему
    экрану: рядом с ними всегда есть кнопки «Назад» и «Меню», и они к
    расписанию не относятся.
    """

    buttons: list[Btn] = field(default_factory=list)

    @classmethod
    def of(cls, message: SentMessage) -> Slots:
        return cls(
            buttons=[
                btn
                for btn in message.buttons()
                if btn.payload and (btn.payload.startswith("t:") or btn.payload.startswith("z:"))
            ]
        )

    @property
    def labels(self) -> list[str]:
        return [btn.text for btn in self.buttons]

    @property
    def free(self) -> list[str]:
        return [btn.text for btn in self.buttons if btn.payload.startswith("t:")]

    @property
    def taken(self) -> list[str]:
        return [btn.text for btn in self.buttons if btn.payload.startswith("z:")]

    @property
    def free_payloads(self) -> list[str]:
        return [btn.payload for btn in self.buttons if btn.payload.startswith("t:")]

    @property
    def taken_payloads(self) -> list[str]:
        return [btn.payload for btn in self.buttons if btn.payload.startswith("z:")]

    def payload_of(self, label: str) -> str:
        for btn in self.buttons:
            if btn.text == label:
                return btn.payload
        raise AssertionError(f"кнопки {label!r} нет среди {self.labels}")


def slots_of(message: SentMessage) -> Slots:
    return Slots.of(message)
