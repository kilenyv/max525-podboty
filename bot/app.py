"""Сборка приложения: бот, диспетчер, репозитории, планировщик.

Один модуль, который показывает весь состав системы: зависимости
собираются здесь и передаются обработчикам через AppContext.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

from maxapi import Bot, Dispatcher
from maxapi.exceptions.max import InvalidToken

from bot.config import Settings, get_settings
from bot.db.connection import Database
from bot.db.repositories import Repositories
from bot.handlers import register_all
from bot.handlers.base import AppContext
from bot.logging_config import get_logger, setup_logging
from bot.services.booking import BookingService
from bot.services.max_sender import MaxSender
from bot.services.notifier import Notifier
from bot.services.scheduler import Scheduler

log = get_logger(__name__)

#: Ключ в таблице bot_state: с него MAX продолжит выдачу обновлений.
MARKER_KEY = "max_update_marker"

#: Как часто сохранять маркер обновлений. Чаще не нужно: при рестарте
#: потеряется максимум несколько секунд истории.
MARKER_SAVE_INTERVAL = 5.0


def build_context(settings: Settings, bot: Bot) -> AppContext:
    """Собирает сервисы приложения. Порядок важен: репозитории → сервисы."""
    db = Database(settings.database_path)
    repos = Repositories(db)

    sender = MaxSender(bot, rate_limit=settings.api_rate_limit)
    booking = BookingService(db, repos)
    notifier = Notifier(db, repos, sender, booking)
    scheduler = Scheduler(
        db=db,
        repos=repos,
        notifier=notifier,
        booking_service=booking,
        poll_interval=settings.jobs_poll_interval,
    )

    return AppContext(
        settings=settings,
        db=db,
        repos=repos,
        sender=sender,
        booking=booking,
        notifier=notifier,
        scheduler=scheduler,
    )


async def build_dispatcher(ctx: AppContext) -> Dispatcher:
    """Диспетчер со всеми обработчиками.

    Сервисы не передаются через data: обработчики закрываются на
    AppContext сами. Промежуточная прослойка ничего не добавила бы,
    только спрятала бы зависимость.
    """
    dp = Dispatcher()
    await register_all(dp, ctx)
    return dp


async def run() -> None:
    """Точка входа: конфигурация, база, планировщик, поллинг MAX."""
    settings = get_settings()
    setup_logging(settings.log_level)
    settings.require()

    bot = Bot(settings.max_bot_token)
    ctx = build_context(settings, bot)

    # close() стоит во внешнем finally, а не во внутреннем: если сборка
    # диспетчера упадёт (например, обработчик не импортировался), поток
    # aiosqlite останется жить и процесс зависнет вместо того, чтобы
    # упасть с трассировкой. Docker такого бота перезапускал бы вечно.
    try:
        await ctx.db.connect()
        dp = await build_dispatcher(ctx)

        await _restore_marker(ctx, bot)
        await _log_profile(bot)

        ctx.scheduler.start()
        marker_task = asyncio.create_task(_save_marker_forever(ctx, bot))
        _install_signal_handlers()

        try:
            await dp.start_polling(bot)
        except InvalidToken as exc:
            # Неверный токен — это не «повторить позже», а опечатка в .env.
            # Traceback в логах контейнера её не объяснит.
            raise RuntimeError(
                "MAX отверг токен. Проверьте MAX_BOT_TOKEN в .env: пробелы, кавычки "
                "и переносы строк внутри токена ломают авторизацию."
            ) from exc
        finally:
            marker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await marker_task
            await ctx.scheduler.stop()
    finally:
        # close_session освобождает aiohttp-коннекторы MAX. Без него
        # при перезапуске (и в тестах) Python печатает «Unclosed connector»
        # в stderr и держит сокеты открытыми до конца процесса.
        with contextlib.suppress(Exception):
            await bot.close_session()
        await ctx.db.close()
        log.info("Бот остановлен")


# --- Маркер обновлений ------------------------------------------------------


async def _restore_marker(ctx: AppContext, bot: Bot) -> None:
    """Продолжает выдачу обновлений с места, где бот остановился.

    MAX хранит очередь обновлений на своей стороне, но недолго: если бот
    молчал дольше, события теряются. Сохранённый маркер избавляет от
    повторной обработки старых нажатий после перезапуска контейнера.
    """
    stored = await ctx.db.get_state_value(MARKER_KEY)
    if not stored:
        return

    try:
        bot.set_marker_updates(int(stored))
    except ValueError:
        log.warning("В базе испорченный маркер обновлений: %r", stored)
    else:
        log.info("Маркер обновлений восстановлен: %s", stored)


async def _save_marker_forever(ctx: AppContext, bot: Bot) -> None:
    """Периодически сохраняет маркер: он меняется прямо в процессе поллинга."""
    while True:
        await asyncio.sleep(MARKER_SAVE_INTERVAL)
        marker = getattr(bot, "marker_updates", None)
        if marker is None:
            continue
        with contextlib.suppress(Exception):
            await ctx.db.set_state_value(MARKER_KEY, str(marker))


async def _log_profile(bot: Bot) -> None:
    """Показывает имя бота в логах. Неудача не должна мешать запуску."""
    try:
        me = await bot.get_me()
    except Exception:  # noqa: BLE001 - профиль не влияет на работу бота
        log.warning("Не удалось получить профиль бота, продолжаю")
    else:
        log.info("Бот запущен: %s (@%s)", me.first_name, me.username)


# --- Остановка --------------------------------------------------------------


def _install_signal_handlers() -> None:
    """Ctrl+C и SIGTERM от контейнера останавливают бота штатно."""
    loop = asyncio.get_running_loop()

    for signal_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, signal_name, None)
        if sig is None:
            continue
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, _request_stop, loop)


def _request_stop(loop: asyncio.AbstractEventLoop) -> None:
    log.info("Получен сигнал остановки")
    for task in asyncio.all_tasks(loop):
        task.cancel()


__all__ = ["MARKER_KEY", "AppContext", "build_context", "build_dispatcher", "run"]
