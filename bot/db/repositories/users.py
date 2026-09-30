"""Репозиторий пользователей MAX.

Пользователь появляется при первом обращении к боту. Регистрации нет:
идентификатор MAX — это и есть ключ доступа к своей админ-панели.
"""

from __future__ import annotations

import sqlite3
import time

from bot.logging_config import get_logger

log = get_logger(__name__)


class UsersRepository:
    """Пользователи MAX: время последнего захода и счётчик подбот за сутки.

    Регистрации нет: строка появляется сама при первом обращении.
    """

    def __init__(self, db) -> None:  # noqa: ANN001
        self.db = db

    async def upsert(
        self,
        user_id: int,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> None:
        now = int(time.time())
        await self.db.execute(
            """
            INSERT INTO users (user_id, username, first_name, last_name, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (user_id) DO UPDATE SET
                username   = COALESCE(excluded.username, users.username),
                first_name = COALESCE(excluded.first_name, users.first_name),
                last_name  = COALESCE(excluded.last_name, users.last_name),
                updated_at = excluded.updated_at
            """,
            (user_id, username, first_name, last_name, now, now),
        )

    async def exists(self, user_id: int) -> bool:
        row = await self.db.fetch_one("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
        return row is not None

    async def get(self, user_id: int) -> sqlite3.Row | None:
        return await self.db.fetch_one("SELECT * FROM users WHERE user_id = ?", (user_id,))
