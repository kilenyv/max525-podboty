"""Репозиторий сессий диалогов.

Сессия — это «на каком шаге находится пользователь и что он уже ввёл».
Хранится в БД, а не в памяти процесса: перезапуск контейнера посреди
ввода данных не обнуляет диалог.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from bot.db.connection import Database

#: Сессия старше суток считается брошенной.
STALE_AFTER_SECONDS = 24 * 3600


class Session:
    """Обёртка над строкой sessions с накоплением данных."""

    __slots__ = ("_store", "_user_id", "data", "state")

    def __init__(
        self,
        store: SessionsRepository,
        user_id: int,
        state: str | None,
        data: dict[str, Any],
    ) -> None:
        self._store = store
        self._user_id = user_id
        self.state = state
        self.data = data

    @property
    def user_id(self) -> int:
        return self._user_id

    async def set_state(self, state: str | None, **data: Any) -> None:
        self.state = state
        if data:
            self.data.update(data)
        await self._store.save(self._user_id, self.state, self.data)

    async def update(self, **data: Any) -> None:
        self.data.update(data)
        await self._store.save(self._user_id, self.state, self.data)

    async def reset(self, **data: Any) -> None:
        """Начать диалог заново с чистым набором данных."""
        self.state = None
        self.data = dict(data)
        await self._store.save(self._user_id, None, self.data)

    async def finish(self) -> None:
        await self.clear()

    async def clear(self) -> None:
        self.state = None
        self.data = {}
        await self._store.delete(self._user_id)


class SessionsRepository:
    """Хранилище диалогов: по одному активному диалогу на пользователя."""

    def __init__(self, db: Database) -> None:
        self.db = db
        # События одного пользователя обрабатываются последовательно,
        # но подстраховываемся от гонки при параллельных апдейтах.
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock_for(self, user_id: int) -> asyncio.Lock:
        lock = self._locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_id] = lock
        return lock

    async def load(self, user_id: int) -> Session:
        row = await self.db.fetch_one(
            "SELECT state, data, updated_at FROM sessions WHERE user_id = ?", (user_id,)
        )
        if row is None:
            return Session(self, user_id, None, {})

        if time.time() - row["updated_at"] > STALE_AFTER_SECONDS:
            await self.delete(user_id)
            return Session(self, user_id, None, {})

        try:
            data = json.loads(row["data"])
        except json.JSONDecodeError:
            data = {}
        if not isinstance(data, dict):
            data = {}

        return Session(self, user_id, row["state"], data)

    async def save(self, user_id: int, state: str | None, data: dict[str, Any]) -> None:
        await self.db.execute(
            """
            INSERT INTO sessions (user_id, state, data, updated_at) VALUES (?, ?, ?, ?)
            ON CONFLICT (user_id) DO UPDATE SET
                state = excluded.state,
                data = excluded.data,
                updated_at = excluded.updated_at
            """,
            (user_id, state, json.dumps(data, ensure_ascii=False), int(time.time())),
        )

    async def delete(self, user_id: int) -> None:
        await self.db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    async def purge_stale(self, before: int | None = None) -> int:
        before = before or int(time.time()) - STALE_AFTER_SECONDS
        cursor = await self.db.execute("DELETE FROM sessions WHERE updated_at < ?", (before,))
        return cursor.rowcount

    def lock(self, user_id: int) -> asyncio.Lock:
        return self._lock_for(user_id)
