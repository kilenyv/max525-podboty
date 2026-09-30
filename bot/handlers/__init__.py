"""Регистрация обработчиков и единая точка ввода текста.

Кнопки привязаны к своим кодам, поэтому порядок их регистрации не важен:
у каждой свой фильтр. А вот обычный текст приходит одним событием
`MESSAGE_CREATED`, и maxapi отдаёт его только первому подошедшему
обработчику. Если бы каждый раздел зарегистрировал свой, сообщения
разбирал бы кто-то один — и диалоги ломались бы в зависимости от
порядка импортов.

Поэтому текст разбирается здесь, в одном месте, и по порядку:
команды → расписание → правила → профиль подбота → форма записи.
Первый, кто понял сообщение, и отвечает за него.
"""

from __future__ import annotations

from typing import Any

from maxapi import Dispatcher

from bot.handlers.admin import handle_profile_text, register_admin
from bot.handlers.admin import rules as rules_section
from bot.handlers.admin import schedule as schedule_section
from bot.handlers.base import AppContext
from bot.handlers.client import handle_form_input, register_client_booking, register_my_bookings
from bot.handlers.start import handle_command, register_start
from bot.logging_config import get_logger

log = get_logger(__name__)


async def register_all(dp: Dispatcher, ctx: AppContext) -> None:
    """Подключает все сценарии бота к диспетчеру."""
    await register_start(dp, ctx)
    await register_client_booking(dp, ctx)
    await register_my_bookings(dp, ctx)
    await register_admin(dp, ctx)
    _register_text_input(dp, ctx)
    log.debug("Обработчики зарегистрированы")


def _register_text_input(dp: Dispatcher, ctx: AppContext) -> None:
    """Единственный обработчик обычных сообщений на весь бот."""

    @dp.message_created()
    async def on_text(event: Any) -> None:
        if await handle_command(ctx, event):
            return
        if await schedule_section.handle_text(ctx, event):
            return
        if await rules_section.handle_text(ctx, event):
            return
        if await handle_profile_text(ctx, event):
            return
        if await handle_form_input(ctx, event):
            return

        # Ни один диалог не начат: помогаем и не молчим.
        await ctx.answer(
            event,
            "Не понял сообщение. Напишите /start — покажу, что умею.",
            None,
        )


__all__ = ["register_all"]
