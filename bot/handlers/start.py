"""Вход в бота: /start, команды и переход по диплинку.

Обработчик текста здесь только разбирает команды. Всё остальное — диалоги
подботов — разбирает единая точка входа в `bot.handlers.register_all`,
поэтому порядок регистрации не влияет на то, кому достанется сообщение.
"""

from __future__ import annotations

from typing import Any

from maxapi.types import BotStarted

from bot.domain import callback as cb
from bot.domain.deeplink import parse_payload
from bot.filters import is_code
from bot.handlers.base import AppContext, message_text, user_id_of, user_of
from bot.logging_config import get_logger
from bot.services import text
from bot.services.ui import Btn, Row

log = get_logger(__name__)

#: Команды, которые понимает бот.
KNOWN_COMMANDS = ("/start", "/help", "/cancel")


def is_command(raw: str) -> bool:
    """Команда ли это: начинается со слэша и не содержит пробелов в начале."""
    return raw.strip().startswith("/")


def command_of(raw: str) -> str:
    """Имя команды без слэша и без суффикса @botname."""
    head = raw.strip().split(maxsplit=1)[0]
    return head.split("@", 1)[0].lower()


async def handle_command(ctx: AppContext, event: Any) -> bool:
    """Обрабатывает команду, если сообщение ею является.

    Возвращает False для обычного текста — тогда его заберёт диалог.
    """
    raw = message_text(event)
    if not is_command(raw):
        return False

    command = command_of(raw)
    log.debug("Команда: %s", command)

    if command == "/start":
        # Полезная нагрузка может прийти и в deep link, и после /start.
        slug = parse_payload(raw[len("/start") :].strip())
        if slug:
            await _open_subbot_by_slug(ctx, event, slug)
        else:
            await _show_menu(ctx, event)
        return True

    if command == "/help":
        await _show_help(ctx, event)
        return True

    if command == "/cancel":
        await _abort_dialog(ctx, event)
        return True

    return False


async def register_start(dp, ctx: AppContext) -> None:  # noqa: ANN001
    """Регистрирует обработчики, работающие с событиями, а не с текстом."""

    @dp.bot_started()
    async def on_bot_started(event: BotStarted) -> None:
        """Пользователь нажал «Начать» или пришёл по ссылке подбота."""
        await _touch_user(ctx, event)
        slug = parse_payload(getattr(event, "payload", None))
        if slug:
            await _open_subbot_by_slug(ctx, event, slug)
            return
        await ctx.show_main_menu(event)

    @dp.message_callback(is_code(cb.MENU_HOME))
    async def on_home(event) -> None:  # noqa: ANN001
        await ctx.show_main_menu(event)

    @dp.message_callback(is_code(cb.MENU_HELP))
    async def on_help(event) -> None:  # noqa: ANN001
        await ctx.show_help(event)


# --- Ответы -----------------------------------------------------------------


async def _show_menu(ctx: AppContext, event: Any) -> None:
    await _touch_user(ctx, event)
    await ctx.show_main_menu(event)


async def _show_help(ctx: AppContext, event: Any) -> None:
    await ctx.show_help(event)


async def _abort_dialog(ctx: AppContext, event: Any) -> None:
    """Выход из любого незавершённого диалога."""
    user_id = user_id_of(event)
    if user_id is not None:
        session = await ctx.session(user_id)
        await session.finish()

    rows: list[Row] = [
        [Btn(text.BUTTON_BOOK, payload=cb.MENU_BOOK)],
        [Btn(text.BUTTON_PANEL, payload=cb.MENU_PANEL)],
    ]
    await ctx.answer(event, "Диалог прерван.", rows)


# --- Пользователь и подботы -------------------------------------------------


async def _touch_user(ctx: AppContext, event: Any) -> None:
    """Запоминает пользователя при первом обращении."""
    user = user_of(event)
    if user is None:
        return

    await ctx.repos.users.upsert(
        user_id=user.user_id,
        username=getattr(user, "username", None),
        first_name=getattr(user, "first_name", None),
        last_name=getattr(user, "last_name", None),
    )


async def _open_subbot_by_slug(ctx: AppContext, event: Any, slug: str) -> None:
    """Открывает подбот по короткой ссылке max.ru/<bot>?start=s_<slug>."""
    subbot = await ctx.repos.subbots.get_by_slug(slug)
    if subbot is None:
        await ctx.answer(
            event,
            text.NOT_A_SUBBOT,
            [[Btn(text.BUTTON_MENU, payload=cb.MENU_HOME)]],
        )
        return

    if not subbot.is_configured:
        await ctx.answer(
            event,
            text.subbot_card(subbot),
            [[Btn(text.BUTTON_MENU, payload=cb.MENU_HOME)]],
        )
        return

    rows: list[Row] = [[Btn("Выбрать время", payload=cb.subbot_open(subbot.id))]]
    await ctx.answer(event, text.subbot_card(subbot), rows)


__all__ = ["KNOWN_COMMANDS", "command_of", "handle_command", "is_command", "register_start"]
