"""Репозиторий подботов: сами страницы записи, их расписание и поля формы.

Изоляция мультиарендности держится здесь: `get_owned` и `get_by_slug`
всегда требуют owner_user_id для операций с настройками. Клиентский путь
(`get_by_slug`) читать записи не может — он возвращает данные страницы,
а не данные владельца.
"""

from __future__ import annotations

import sqlite3
import time

from bot.db.connection import Database
from bot.domain.enums import DEFAULT_FIELDS, BookingStatus, FieldType
from bot.domain.models import FormField, Interval, Subbot
from bot.domain.slug import build_slug_candidates
from bot.domain.timeutil import MINUTES_IN_DAY

_SUBBOT_COLUMNS = """
    id, owner_user_id, slug, title, description, timezone, slot_minutes,
    horizon_days, cancel_notice_hours, reminder_hours, digest_hour,
    is_active, created_at, updated_at
"""


class SubbotsRepository:
    """Страницы записи: профиль, расписание, поля формы и правила.

    Все проверки владения подботом живут здесь, а не в обработчиках:
    чужим пользователям недоступно даже знание, что подбот существует.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    # --- Чтение ----------------------------------------------------------

    def _hydrate(self, row: sqlite3.Row) -> Subbot:
        return Subbot(
            id=row["id"],
            owner_user_id=row["owner_user_id"],
            slug=row["slug"],
            title=row["title"],
            description=row["description"] or "",
            timezone=row["timezone"],
            slot_minutes=row["slot_minutes"],
            horizon_days=row["horizon_days"],
            cancel_notice_hours=row["cancel_notice_hours"],
            reminder_hours=row["reminder_hours"],
            digest_hour=row["digest_hour"],
            is_active=bool(row["is_active"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def get_by_id(self, subbot_id: int) -> Subbot | None:
        row = await self.db.fetch_one(f"SELECT {_SUBBOT_COLUMNS} FROM subbots WHERE id = ?", (subbot_id,))
        if row is None:
            return None
        return await self._with_schedule(self._hydrate(row))

    async def get_by_slug(self, slug: str) -> Subbot | None:
        row = await self.db.fetch_one(
            f"SELECT {_SUBBOT_COLUMNS} FROM subbots WHERE slug = ? AND is_active = 1", (slug,)
        )
        if row is None:
            return None
        return await self._with_schedule(self._hydrate(row))

    async def get_owned(self, subbot_id: int, owner_user_id: int) -> Subbot | None:
        """Подбот, если он принадлежит пользователю. Иначе None.

        Это единственная точка контроля доступа к настройкам.
        """
        row = await self.db.fetch_one(
            f"SELECT {_SUBBOT_COLUMNS} FROM subbots WHERE id = ? AND owner_user_id = ?",
            (subbot_id, owner_user_id),
        )
        if row is None:
            return None
        return await self._with_schedule(self._hydrate(row))

    async def _with_schedule(self, subbot: Subbot) -> Subbot:
        rows = await self.db.fetch_all(
            """
            SELECT id, weekday, start_min, end_min FROM work_intervals
            WHERE subbot_id = ? ORDER BY weekday, start_min
            """,
            (subbot.id,),
        )
        for row in rows:
            subbot.intervals.setdefault(row["weekday"], []).append(
                Interval(start=row["start_min"], end=row["end_min"], id=row["id"])
            )
            subbot.workdays.add(row["weekday"])

        break_rows = await self.db.fetch_all(
            """
            SELECT id, weekday, start_min, end_min FROM breaks
            WHERE subbot_id = ? ORDER BY weekday, start_min
            """,
            (subbot.id,),
        )
        for row in break_rows:
            subbot.breaks.setdefault(row["weekday"], []).append(
                Interval(start=row["start_min"], end=row["end_min"], id=row["id"])
            )

        field_rows = await self.db.fetch_all(
            """
            SELECT id, subbot_id, field_type, label, is_required, position, max_length
            FROM form_fields WHERE subbot_id = ? ORDER BY position, id
            """,
            (subbot.id,),
        )
        subbot.form_fields = [
            FormField(
                id=row["id"],
                subbot_id=row["subbot_id"],
                field_type=FieldType(row["field_type"]),
                label=row["label"],
                is_required=bool(row["is_required"]),
                position=row["position"],
                max_length=row["max_length"],
            )
            for row in field_rows
        ]
        return subbot

    async def list_owned(self, owner_user_id: int) -> list[Subbot]:
        rows = await self.db.fetch_all(
            f"SELECT {_SUBBOT_COLUMNS} FROM subbots WHERE owner_user_id = ? ORDER BY created_at",
            (owner_user_id,),
        )
        return [await self._with_schedule(self._hydrate(row)) for row in rows]

    async def count_owned(self, owner_user_id: int) -> int:
        row = await self.db.fetch_one(
            "SELECT COUNT(*) AS total FROM subbots WHERE owner_user_id = ?", (owner_user_id,)
        )
        return row["total"] if row else 0

    async def count_created_today(self, owner_user_id: int) -> int:
        row = await self.db.fetch_one(
            """
            SELECT COUNT(*) AS total FROM subbots
            WHERE owner_user_id = ? AND created_at >= strftime('%s','now','start of day')
            """,
            (owner_user_id,),
        )
        return row["total"] if row else 0

    async def list_active_ids(self) -> list[int]:
        rows = await self.db.fetch_all("SELECT id FROM subbots WHERE is_active = 1")
        return [row["id"] for row in rows]

    # --- Создание и изменение --------------------------------------------

    async def create(
        self,
        owner_user_id: int,
        title: str,
        description: str,
        timezone: str,
    ) -> Subbot:
        """Создаёт подбот с уникальным слагом и набором полей по умолчанию."""
        async with self.db.transaction(immediate=True) as conn:
            slug = await self._pick_slug(conn, title)
            now = int(time.time())
            cursor = await conn.execute(
                """
                INSERT INTO subbots (
                    owner_user_id, slug, title, description, timezone,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (owner_user_id, slug, title.strip(), description.strip(), timezone, now, now),
            )
            subbot_id = cursor.lastrowid

            for position, (field_type, required) in enumerate(DEFAULT_FIELDS):
                from bot.domain.enums import FIELD_LABELS, MAX_TEXT_LENGTH

                max_length = MAX_TEXT_LENGTH if field_type is FieldType.COMMENT else 255
                await conn.execute(
                    """
                    INSERT INTO form_fields
                        (subbot_id, field_type, label, is_required, position, max_length)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        subbot_id,
                        field_type.value,
                        FIELD_LABELS[field_type],
                        int(required),
                        position,
                        max_length,
                    ),
                )

        created = await self.get_by_id(subbot_id)
        assert created is not None  # только что вставлено
        return created

    async def _pick_slug(self, conn: sqlite3.Connection, title: str) -> str:
        for candidate in build_slug_candidates(title):
            row = await conn.execute("SELECT 1 FROM subbots WHERE slug = ?", (candidate,))
            if await row.fetchone() is None:
                return candidate
        raise RuntimeError("Не удалось сгенерировать уникальный слаг")

    async def slug_exists(self, slug: str) -> bool:
        row = await self.db.fetch_one("SELECT 1 FROM subbots WHERE slug = ?", (slug,))
        return row is not None

    async def update_profile(
        self,
        subbot_id: int,
        owner_user_id: int,
        title: str | None = None,
        description: str | None = None,
    ) -> bool:
        sets, params = [], []
        if title is not None:
            sets.append("title = ?")
            params.append(title.strip())
        if description is not None:
            sets.append("description = ?")
            params.append(description.strip())
        if not sets:
            return False
        sets.append("updated_at = strftime('%s','now')")
        params.extend([subbot_id, owner_user_id])
        await self.db.execute(
            f"UPDATE subbots SET {', '.join(sets)} WHERE id = ? AND owner_user_id = ?",
            tuple(params),
        )
        return True

    async def update_rules(
        self,
        subbot_id: int,
        owner_user_id: int,
        **values: int | str,
    ) -> bool:
        allowed = {
            "timezone",
            "slot_minutes",
            "horizon_days",
            "cancel_notice_hours",
            "reminder_hours",
            "digest_hour",
            "is_active",
        }
        sets, params = [], []
        for key, value in values.items():
            if key not in allowed:
                continue
            sets.append(f"{key} = ?")
            params.append(int(value) if isinstance(value, bool) else value)
        if not sets:
            return False
        sets.append("updated_at = strftime('%s','now')")
        params.extend([subbot_id, owner_user_id])
        await self.db.execute(
            f"UPDATE subbots SET {', '.join(sets)} WHERE id = ? AND owner_user_id = ?",
            tuple(params),
        )
        return True

    async def delete(self, subbot_id: int, owner_user_id: int) -> bool:
        cursor = await self.db.execute(
            "DELETE FROM subbots WHERE id = ? AND owner_user_id = ?", (subbot_id, owner_user_id)
        )
        return cursor.rowcount > 0

    # --- Расписание ------------------------------------------------------

    async def clear_day(self, subbot_id: int, owner_user_id: int, weekday: int) -> None:
        """Убирает день из расписания вместе с перерывами."""
        async with self.db.transaction(immediate=True) as conn:
            await self._assert_owned(conn, subbot_id, owner_user_id)
            await conn.execute(
                "DELETE FROM work_intervals WHERE subbot_id = ? AND weekday = ?",
                (subbot_id, weekday),
            )
            await conn.execute("DELETE FROM breaks WHERE subbot_id = ? AND weekday = ?", (subbot_id, weekday))

    async def add_interval(
        self, subbot_id: int, owner_user_id: int, weekday: int, start: int, end: int
    ) -> int:
        async with self.db.transaction(immediate=True) as conn:
            await self._assert_owned(conn, subbot_id, owner_user_id)
            await self._assert_no_overlap(conn, subbot_id, weekday, start, end, "work_intervals")
            cursor = await conn.execute(
                """
                INSERT INTO work_intervals (subbot_id, weekday, start_min, end_min, position)
                VALUES (?, ?, ?, ?,
                        (SELECT COALESCE(MAX(position), -1) + 1
                           FROM work_intervals WHERE subbot_id = ? AND weekday = ?))
                """,
                (subbot_id, weekday, start, end, subbot_id, weekday),
            )
            return cursor.lastrowid

    async def add_break(self, subbot_id: int, owner_user_id: int, weekday: int, start: int, end: int) -> int:
        async with self.db.transaction(immediate=True) as conn:
            await self._assert_owned(conn, subbot_id, owner_user_id)
            await self._assert_no_overlap(conn, subbot_id, weekday, start, end, "breaks")
            cursor = await conn.execute(
                """
                INSERT INTO breaks (subbot_id, weekday, start_min, end_min)
                VALUES (?, ?, ?, ?)
                """,
                (subbot_id, weekday, start, end),
            )
            return cursor.lastrowid

    async def delete_interval(
        self, subbot_id: int, owner_user_id: int, weekday: int, interval_id: int
    ) -> bool:
        return await self._delete_owned(
            "DELETE FROM work_intervals WHERE id = ? AND subbot_id = ? AND weekday = ?",
            interval_id,
            subbot_id,
            owner_user_id,
            weekday,
        )

    async def delete_break(self, subbot_id: int, owner_user_id: int, weekday: int, break_id: int) -> bool:
        return await self._delete_owned(
            "DELETE FROM breaks WHERE id = ? AND subbot_id = ? AND weekday = ?",
            break_id,
            subbot_id,
            owner_user_id,
            weekday,
        )

    async def _delete_owned(
        self, sql: str, row_id: int, subbot_id: int, owner_user_id: int, weekday: int
    ) -> bool:
        async with self.db.transaction(immediate=True) as conn:
            await self._assert_owned(conn, subbot_id, owner_user_id)
            cursor = await conn.execute(sql, (row_id, subbot_id, weekday))
            return cursor.rowcount > 0

    async def _assert_owned(self, conn: sqlite3.Connection, subbot_id: int, owner_user_id: int) -> None:
        row = await conn.execute(
            "SELECT 1 FROM subbots WHERE id = ? AND owner_user_id = ?", (subbot_id, owner_user_id)
        )
        if await row.fetchone() is None:
            raise PermissionError("Подбот не принадлежит пользователю")

    @staticmethod
    async def _assert_no_overlap(
        conn: sqlite3.Connection, subbot_id: int, weekday: int, start: int, end: int, table: str
    ) -> None:
        """Внутри одной таблицы записи не должны налезать друг на друга.

        Проверяется только своя таблица: перерыв внутри рабочего интервала
        — это как раз обычное дело, а вот два перерыва поверх другого
        или два рабочих интервала с одними часами — ошибка расписания.
        """
        cursor = await conn.execute(
            f"""
            SELECT start_min, end_min FROM {table}
            WHERE subbot_id = ? AND weekday = ? AND start_min < ? AND end_min > ?
            LIMIT 1
            """,
            (subbot_id, weekday, end, start),
        )
        if await cursor.fetchone() is not None:
            raise ValueError("Интервал пересекается с уже добавленным")

    # --- Поля формы ------------------------------------------------------

    async def set_field_enabled(
        self,
        subbot_id: int,
        owner_user_id: int,
        field_type: FieldType,
        enabled: bool,
        is_required: bool | None = None,
    ) -> None:
        async with self.db.transaction(immediate=True) as conn:
            await self._assert_owned(conn, subbot_id, owner_user_id)
            row = await conn.execute(
                "SELECT id FROM form_fields WHERE subbot_id = ? AND field_type = ?",
                (subbot_id, field_type.value),
            )
            existing = await row.fetchone()

            if existing is None:
                if not enabled:
                    return
                from bot.domain.enums import FIELD_LABELS, MAX_TEXT_LENGTH

                max_length = MAX_TEXT_LENGTH if field_type is FieldType.COMMENT else 255
                row = await conn.execute(
                    "SELECT COALESCE(MAX(position), -1) + 1 AS pos FROM form_fields WHERE subbot_id = ?",
                    (subbot_id,),
                )
                position = (await row.fetchone())["pos"]
                await conn.execute(
                    """
                    INSERT INTO form_fields
                        (subbot_id, field_type, label, is_required, position, max_length)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        subbot_id,
                        field_type.value,
                        FIELD_LABELS[field_type],
                        int(bool(is_required)),
                        position,
                        max_length,
                    ),
                )
                return

            if enabled:
                sets = ["label = label"]
                params: list = []
                if is_required is not None:
                    sets.append("is_required = ?")
                    params.append(int(is_required))
                params.append(existing["id"])
                await conn.execute(f"UPDATE form_fields SET {', '.join(sets)} WHERE id = ?", tuple(params))
            else:
                await conn.execute("DELETE FROM form_fields WHERE id = ?", (existing["id"],))

    async def set_field_required(
        self, subbot_id: int, owner_user_id: int, field_type: FieldType, is_required: bool
    ) -> bool:
        async with self.db.transaction(immediate=True) as conn:
            await self._assert_owned(conn, subbot_id, owner_user_id)
            cursor = await conn.execute(
                """
                UPDATE form_fields SET is_required = ?
                WHERE subbot_id = ? AND field_type = ?
                """,
                (int(is_required), subbot_id, field_type.value),
            )
            return cursor.rowcount > 0

    async def reindex_fields(self, subbot_id: int, owner_user_id: int) -> None:
        async with self.db.transaction(immediate=True) as conn:
            await self._assert_owned(conn, subbot_id, owner_user_id)
            rows = await conn.execute(
                "SELECT id FROM form_fields WHERE subbot_id = ? ORDER BY position, id", (subbot_id,)
            )
            for position, row in enumerate(await rows.fetchall()):
                await conn.execute("UPDATE form_fields SET position = ? WHERE id = ?", (position, row["id"]))

    # --- Прочее ----------------------------------------------------------

    async def count_active_bookings(self, subbot_id: int) -> int:
        row = await self.db.fetch_one(
            """
            SELECT COUNT(*) AS total FROM bookings
            WHERE subbot_id = ? AND status = ? AND starts_at >= strftime('%s','now')
            """,
            (subbot_id, BookingStatus.ACTIVE.value),
        )
        return row["total"] if row else 0

    async def distinct_digest_hours(self) -> list[tuple[int, int]]:
        """Часы дайджеста и владельцы, которым их нужно слать."""
        rows = await self.db.fetch_all(
            """
            SELECT DISTINCT digest_hour, owner_user_id
            FROM subbots WHERE is_active = 1 AND reminder_hours >= 0
            """,
        )
        return [(row["digest_hour"], row["owner_user_id"]) for row in rows]

    @staticmethod
    def clamp_minutes(value: int) -> int:
        return max(5, min(MINUTES_IN_DAY, value))
