"""Изолированный слой общения с мессенджером MAX.

Весь остальной код работает только через `MaxSender` и не знает про
HTTP-детали библиотеки. Здесь же живут троттлинг и повторы.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from bot.logging_config import get_logger
from bot.services.ui import Row, build

log = get_logger(__name__)

#: API MAX ограничивает 30 запросов в секунду на бота.
DEFAULT_RATE_LIMIT = 25


class RateLimiter:
    """Token bucket: ровный поток запросов без всплесков.

    asyncio.Semaphore дал бы «пакет» из N запросов подряд и упирался бы
    в лимит. Здесь каждый запрос ждёт свою долю секунды.
    """

    def __init__(self, rate_per_second: int) -> None:
        self._interval = 1.0 / max(1, rate_per_second)
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait_for = self._next_allowed - now
            if wait_for > 0:
                await asyncio.sleep(wait_for)
                now = time.monotonic()
            self._next_allowed = now + self._interval


class MaxSender:
    """Отправка сообщений с ограничением скорости и повторами."""

    def __init__(self, bot: Any, rate_limit: int = DEFAULT_RATE_LIMIT, retries: int = 3) -> None:
        self._bot = bot
        self._limiter = RateLimiter(rate_limit)
        self._retries = retries

    @property
    def bot(self) -> Any:
        return self._bot

    async def send(
        self,
        chat_id: int,
        text: str,
        rows: list[Row] | None = None,
        *,
        notify: bool | None = None,
    ) -> bool:
        """Отправляет сообщение с клавиатурой. False — отправка не удалась.

        Клавиатуру передаём как список строк кнопок; превращение в формат
        MAX происходит здесь, чтобы обработчики не знали про библиотеку.

        Ошибку наружу не пробрасываем: уведомление не должно ронять
        обработчик записи, поэтому неудачу просто логируем.
        """
        from maxapi.enums.parse_mode import ParseMode

        attachments = build(rows)
        for attempt in range(1, self._retries + 1):
            await self._limiter.acquire()
            try:
                result = await self._bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    attachments=attachments,
                    parse_mode=ParseMode.HTML,
                    notify=notify,
                )
            except Exception as exc:  # noqa: BLE001 - MAX бросает разное, все равно false
                log.warning("Отправка в чат %s не удалась (попытка %d): %s", chat_id, attempt, exc)
                if attempt == self._retries:
                    return False
                await asyncio.sleep(0.5 * attempt)
                continue

            if self._is_error(result):
                log.warning("MAX вернул ошибку для чата %s: %s", chat_id, self._describe(result))
                return False
            return True

        return False

    @staticmethod
    def _is_error(result: Any) -> bool:
        if result is None:
            return False
        try:
            from maxapi.types.errors import Error

            return isinstance(result, Error)
        except ImportError:  # pragma: no cover - зависит от версии библиотеки
            return type(result).__name__ == "Error"

    @staticmethod
    def _describe(result: Any) -> str:
        raw = getattr(result, "raw", None)
        if isinstance(raw, dict):
            return str(raw)
        return repr(result)
