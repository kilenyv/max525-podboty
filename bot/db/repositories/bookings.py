"""Репозиторий записей.

Бронирование — единственная операция, где важна целостность: два клиента
не должны получить один слот. Поэтому создание идёт в `BEGIN IMMEDIATE`
и внутри перепроверяет пересечения.
"""

from __future__ import annotations

import sqlite3
import time

from bot.db.connection import Database
from bot.domain.enums import BookingStatus, CancelSource
from bot.domain.models import Booking

_BOOKING_COLUMNS = """
    id, subbot_id, user_id, starts_at, ends_at, timezone, status,
    created_at, updated_at, cancelled_at, cancel_source
"""


class SlotUnavailableError(RuntimeError):
    """Слот занят или уже прошёл."""


class BookingsRepository:
    """Записи клиентов. Единственный владелец SQL записи и её отмены."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def _hydrate(self, row: sqlite3.Row, values: dict[int, str] | None = None) -> Booking:
        return Booking(
            id=row["id"],
            subbot_id=row["subbot_id"],
            user_id=row["user_id"],
            starts_at=row["starts_at"],
            ends_at=row["ends_at"],
            timezone=row["timezone"],
            status=BookingStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            cancelled_at=row["cancelled_at"],
            cancel_source=row["cancel_source"],
            values=values or {},
        )

    # --- Создание --------------------------------------------------------

    async def create(
        self,
        subbot_id: int,
        user_id: int,
        starts_at: int,
        ends_at: int,
        timezone: str,
        field_values: dict[int, str],
        now: int | None = None,
    ) -> Booking:
        """Создаёт запись. Бросает SlotUnavailableError, если слот пересекается с чужим."""
        now = int(time.time()) if now is None else now
        if starts_at <= now:
            raise SlotUnavailableError("Это время уже прошло")

        async with self.db.transaction(immediate=True) as conn:
            conflict = await conn.execute(
                """
                SELECT id FROM bookings
                WHERE subbot_id = ? AND status = 'active'
                  AND starts_at < ? AND ends_at > ?
                LIMIT 1
                """,
                (subbot_id, ends_at, starts_at),
            )
            if await conflict.fetchone() is not None:
                raise SlotUnavailableError("Время уже занято")

            cursor = await conn.execute(
                """
                INSERT INTO bookings
                    (subbot_id, user_id, starts_at, ends_at, timezone, status,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (subbot_id, user_id, starts_at, ends_at, timezone, now, now),
            )
            booking_id = cursor.lastrowid

            for field_id, value in field_values.items():
                await conn.execute(
                    "INSERT OR REPLACE INTO booking_values (booking_id, field_id, value) VALUES (?, ?, ?)",
                    (booking_id, field_id, value),
                )

        created = await self.get_by_id(booking_id)
        assert created is not None
        return created

    # --- Чтение ----------------------------------------------------------

    async def get_by_id(self, booking_id: int, with_values: bool = True) -> Booking | None:
        row = await self.db.fetch_one(f"SELECT {_BOOKING_COLUMNS} FROM bookings WHERE id = ?", (booking_id,))
        if row is None:
            return None
        values = await self._load_values(booking_id) if with_values else {}
        return self._hydrate(row, values)

    async def get_owned(
        self, booking_id: int, owner_user_id: int, with_values: bool = True
    ) -> Booking | None:
        """Запись, если подбот принадлежит пользователю. Иначе None."""
        row = await self.db.fetch_one(
            f"""
            SELECT {_BOOKING_COLUMNS} FROM bookings
            WHERE id = ?
              AND subbot_id IN (SELECT id FROM subbots WHERE owner_user_id = ?)
            """,
            (booking_id, owner_user_id),
        )
        if row is None:
            return None
        values = await self._load_values(booking_id) if with_values else {}
        return self._hydrate(row, values)

    async def get_active_by_user_slot(self, subbot_id: int, user_id: int, starts_at: int) -> Booking | None:
        """Защита от двойной записи одного клиента на один и тот же слот."""
        row = await self.db.fetch_one(
            f"""
            SELECT {_BOOKING_COLUMNS} FROM bookings
            WHERE subbot_id = ? AND user_id = ? AND starts_at = ? AND status = 'active'
            """,
            (subbot_id, user_id, starts_at),
        )
        return self._hydrate(row) if row else None

    async def _load_values(self, booking_id: int) -> dict[int, str]:
        rows = await self.db.fetch_all(
            "SELECT field_id, value FROM booking_values WHERE booking_id = ?", (booking_id,)
        )
        return {row["field_id"]: row["value"] for row in rows}

    async def busy_windows(self, subbot_id: int, since: int, until: int) -> list[tuple[int, int]]:
        """Занятые отрезки подбота в окне времени — источник занятости для слотов."""
        rows = await self.db.fetch_all(
            """
            SELECT starts_at, ends_at FROM bookings
            WHERE subbot_id = ? AND status = 'active' AND starts_at < ? AND ends_at > ?
            """,
            (subbot_id, until, since),
        )
        return [(row["starts_at"], row["ends_at"]) for row in rows]

    async def list_user_upcoming(
        self, user_id: int, now: int | None = None, limit: int = 20
    ) -> list[Booking]:
        now = int(time.time()) if now is None else now
        rows = await self.db.fetch_all(
            f"""
            SELECT {_BOOKING_COLUMNS} FROM bookings
            WHERE user_id = ? AND status = 'active' AND ends_at > ?
            ORDER BY starts_at LIMIT ?
            """,
            (user_id, now, limit),
        )
        return [await self._hydrate_with_values(row) for row in rows]

    async def list_user_history(self, user_id: int, now: int, limit: int = 10) -> list[Booking]:
        rows = await self.db.fetch_all(
            f"""
            SELECT {_BOOKING_COLUMNS} FROM bookings
            WHERE user_id = ? AND (status != 'active' OR ends_at <= ?)
            ORDER BY starts_at DESC LIMIT ?
            """,
            (user_id, now, limit),
        )
        return [await self._hydrate_with_values(row) for row in rows]

    async def list_subbot_upcoming(
        self, subbot_id: int, now: int | None = None, limit: int = 50
    ) -> list[Booking]:
        now = int(time.time()) if now is None else now
        rows = await self.db.fetch_all(
            f"""
            SELECT {_BOOKING_COLUMNS} FROM bookings
            WHERE subbot_id = ? AND status = 'active' AND starts_at >= ?
            ORDER BY starts_at LIMIT ?
            """,
            (subbot_id, now, limit),
        )
        return [await self._hydrate_with_values(row) for row in rows]

    async def list_subbot_day(self, subbot_id: int, day_start: int, day_end: int) -> list[Booking]:
        rows = await self.db.fetch_all(
            f"""
            SELECT {_BOOKING_COLUMNS} FROM bookings
            WHERE subbot_id = ? AND status = 'active' AND starts_at < ? AND ends_at > ?
            ORDER BY starts_at
            """,
            (subbot_id, day_end, day_start),
        )
        return [await self._hydrate_with_values(row) for row in rows]

    async def list_for_digest(self, owner_user_id: int, day_start: int, day_end: int) -> list[Booking]:
        """Записи владельца за локальные сутки — утренний дайджест."""
        rows = await self.db.fetch_all(
            f"""
            SELECT {_BOOKING_COLUMNS} FROM bookings b
            WHERE b.status = 'active'
              AND b.starts_at < ? AND b.ends_at > ?
              AND b.subbot_id IN (SELECT id FROM subbots WHERE owner_user_id = ?)
            ORDER BY b.starts_at
            """,
            (day_end, day_start, owner_user_id),
        )
        return [await self._hydrate_with_values(row) for row in rows]

    async def _hydrate_with_values(self, row: sqlite3.Row) -> Booking:
        return self._hydrate(row, await self._load_values(row["id"]))

    # --- Изменение статуса -----------------------------------------------

    async def cancel(
        self,
        booking_id: int,
        source: CancelSource,
        now: int | None = None,
    ) -> Booking | None:
        now = int(time.time()) if now is None else now
        cursor = await self.db.execute(
            """
            UPDATE bookings
            SET status = 'cancelled', cancelled_at = ?, cancel_source = ?, updated_at = ?
            WHERE id = ? AND status = 'active'
            """,
            (now, source.value, now, booking_id),
        )
        if not cursor.rowcount:
            return None
        return await self.get_by_id(booking_id)

    async def mark_done(self, before: int) -> int:
        """Переводит в «выполнено» записи, которые уже закончились."""
        cursor = await self.db.execute(
            """
            UPDATE bookings SET status = 'done', updated_at = strftime('%s','now')
            WHERE status = 'active' AND ends_at <= ?
            """,
            (before,),
        )
        return cursor.rowcount

    async def cancel_all_future(self, subbot_id: int) -> list[Booking]:
        """Все будущие записи подбота — нужно при удалении подбота."""
        rows = await self.db.fetch_all(
            """
            SELECT id FROM bookings
            WHERE subbot_id = ? AND status = 'active' AND starts_at >= strftime('%s','now')
            """,
            (subbot_id,),
        )
        cancelled: list[Booking] = []
        for row in rows:
            booking = await self.cancel(row["id"], CancelSource.OWNER)
            if booking is not None:
                cancelled.append(booking)
        return cancelled

    async def find_values_by_field(self, field_id: int, value: str) -> list[int]:
        """Кто уже оставлял такое значение — пригодится для антиспама."""
        rows = await self.db.fetch_all(
            "SELECT booking_id FROM booking_values WHERE field_id = ? AND value = ?",
            (field_id, value),
        )
        return [row["booking_id"] for row in rows]
