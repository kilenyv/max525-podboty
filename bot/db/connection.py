"""Подключение к SQLite и применение миграций.

Один файл БД, WAL-режим и ручные транзакции. WAL даёт читателям и
писателю не мешать друг другу, а `BEGIN IMMEDIATE` в create_booking
сериализует конкурентные брони одного слота.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from bot.logging_config import get_logger

log = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

_MIGRATION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    name       TEXT PRIMARY KEY,
    applied_at INTEGER NOT NULL
)
"""


class Database:
    """Обёртка над aiosqlite с одним соединением на процесс."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("База данных не инициализирована. Вызовите connect().")
        return self._conn

    async def connect(self) -> None:
        if self.path.parent and str(self.path.parent) not in ("", "."):
            self.path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = await aiosqlite.connect(self.path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self.migrate()
        log.info("База данных готова: %s", self.path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def migrate(self) -> None:
        await self._conn.execute(_MIGRATION_TABLE)
        cursor = await self._conn.execute("SELECT name FROM schema_migrations")
        applied = {row["name"] for row in await cursor.fetchall()}

        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue
            sql = path.read_text(encoding="utf-8")
            await self._conn.executescript(sql)
            await self._conn.execute(
                "INSERT INTO schema_migrations (name, applied_at) VALUES (?, strftime('%s','now'))",
                (path.name,),
            )
            log.info("Применена миграция %s", path.name)

    @asynccontextmanager
    async def transaction(self, immediate: bool = False) -> AsyncIterator[aiosqlite.Connection]:
        """Транзакция. immediate=True блокирует писателей — нужно для брони."""
        await self.conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.conn
        except Exception:
            await self.conn.execute("ROLLBACK")
            raise
        else:
            await self.conn.execute("COMMIT")

    async def fetch_all(self, query: str, params: tuple = ()) -> list[sqlite3.Row]:
        async with self.conn.execute(query, params) as cursor:
            return list(await cursor.fetchall())

    async def fetch_one(self, query: str, params: tuple = ()) -> sqlite3.Row | None:
        async with self.conn.execute(query, params) as cursor:
            return await cursor.fetchone()

    async def execute(self, query: str, params: tuple = ()) -> aiosqlite.Cursor:
        """Запрос без чтения строк.

        Возвращает курсор: вызывающему нужен либо `lastrowid` вставки,
        либо `rowcount` — сколько строк задела UPDATE или DELETE.
        """
        return await self.conn.execute(query, params)

    async def set_state_value(self, key: str, value: str) -> None:
        await self.execute(
            """
            INSERT INTO bot_state (key, value, updated_at) VALUES (?, ?, strftime('%s','now'))
            ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value),
        )

    async def get_state_value(self, key: str) -> str | None:
        row = await self.fetch_one("SELECT value FROM bot_state WHERE key = ?", (key,))
        return row["value"] if row else None
