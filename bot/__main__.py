"""Запуск бота: `python -m bot`."""

from __future__ import annotations

import asyncio
import contextlib
import sys

from bot.app import run
from bot.config import get_settings
from bot.logging_config import get_logger, setup_logging

log = get_logger(__name__)


def main() -> int:
    # Ошибки настройки и авторизации — это опечатка в .env, а не сбой
    # программы. Traceback в логах контейнера только мешает её найти,
    # поэтому такие случаи показываются одной строкой и кодом 1.
    #
    # Логи настраиваются здесь, до run(): настройка одноразовая, и если
    # сначала вызвать её с умолчанием, LOG_LEVEL из .env потеряется.
    setup_logging(get_settings().log_level)
    try:
        # Ctrl+C в терминале — штатная остановка, а не ошибка.
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(run())
    except (RuntimeError, OSError) as exc:
        log.error("Бот не запустился: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
