"""Очередь фоновых задач.

Напоминания клиентам и утренние дайджесты владельцам — это отложенные
действия, которые должны пережить перезапуск. Поэтому очередь в БД,
а не таймеры в памяти: подняли контейнер — задачи на месте.

Ключ дедупликации не даёт поставить одну и ту же задачу дважды.
"""

from __future__ import annotations

import json
import time
from typing import Any

from bot.db.connection import Database
from bot.domain.enums import JobKind

MAX_ATTEMPTS = 3
#: На сколько секунд откладывается неудачная попытка. Отправка могла не
#: дойти из-за сети — на одиночную попытку напоминание просто потерялось бы.
RETRY_DELAY = 60


class Job:
    """Задача из очереди: что, когда запустить и с каким payload."""

    __slots__ = ("attempts", "dedup_key", "id", "kind", "payload", "run_at")

    def __init__(self, row) -> None:  # noqa: ANN001 - sqlite3.Row
        self.id: int = row["id"]
        self.kind: JobKind = JobKind(row["kind"])
        self.dedup_key: str = row["dedup_key"]
        self.run_at: int = row["run_at"]
        self.attempts: int = row["attempts"]
        try:
            self.payload: dict[str, Any] = json.loads(row["payload"])
        except (json.JSONDecodeError, TypeError):
            self.payload = {}

    def __repr__(self) -> str:  # pragma: no cover - для логов
        return f"<Job {self.kind}:{self.dedup_key} at={self.run_at}>"


class JobsRepository:
    """Очередь фоновых задач с дедупликацией и повторами при сбоях."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def schedule(
        self, kind: JobKind, dedup_key: str, run_at: int, payload: dict[str, Any] | None = None
    ) -> None:
        """Ставит задачу в очередь. Повторный вызов с тем же ключом игнорируется."""
        await self.db.execute(
            """
            INSERT INTO jobs (kind, dedup_key, run_at, payload, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
            ON CONFLICT (dedup_key) DO UPDATE SET
                run_at = excluded.run_at,
                payload = excluded.payload,
                status = 'pending',
                attempts = 0
            """,
            (
                kind.value,
                dedup_key,
                int(run_at),
                json.dumps(payload or {}, ensure_ascii=False),
                int(time.time()),
            ),
        )

    async def cancel(self, dedup_key: str) -> None:
        await self.db.execute(
            "UPDATE jobs SET status = 'cancelled' WHERE dedup_key = ? AND status = 'pending'",
            (dedup_key,),
        )

    async def cancel_prefix(self, prefix: str) -> int:
        cursor = await self.db.execute(
            "UPDATE jobs SET status = 'cancelled' WHERE dedup_key LIKE ? AND status = 'pending'",
            (f"{prefix}%",),
        )
        return cursor.rowcount

    async def due(self, now: int | None = None, limit: int = 20) -> list[Job]:
        now = int(time.time()) if now is None else now
        rows = await self.db.fetch_all(
            """
            SELECT * FROM jobs
            WHERE status = 'pending' AND run_at <= ?
            ORDER BY run_at LIMIT ?
            """,
            (now, limit),
        )
        return [Job(row) for row in rows]

    async def has_due(self, now: int | None = None) -> bool:
        now = int(time.time()) if now is None else now
        row = await self.db.fetch_one(
            "SELECT 1 FROM jobs WHERE status = 'pending' AND run_at <= ? LIMIT 1", (now,)
        )
        return row is not None

    async def mark_done(self, job: Job) -> None:
        await self.db.execute(
            "UPDATE jobs SET status = 'done', done_at = ?, attempts = attempts + 1 WHERE id = ?",
            (int(time.time()), job.id),
        )

    async def mark_failed(self, job: Job) -> bool:
        """Записывает неудачную попытку.

        Пока попытки не исчерпаны, задача остаётся в очереди и переносится
        на RETRY_DELAY: сетевой сбой не должен съесть напоминание. Когда
        попытки кончились, задача окончательно помечается failed.

        Возвращает True, если это была последняя попытка.
        """
        attempts = job.attempts + 1
        last_attempt = attempts >= MAX_ATTEMPTS
        await self.db.execute(
            """
            UPDATE jobs
            SET status = ?, attempts = ?, done_at = ?, run_at = ?
            WHERE id = ?
            """,
            (
                "failed" if last_attempt else "pending",
                attempts,
                int(time.time()) if last_attempt else None,
                job.run_at + RETRY_DELAY * attempts,
                job.id,
            ),
        )
        return last_attempt

    async def pending(self, kind: JobKind | None = None) -> list[Job]:
        """Все ещё не выполненные задачи — для проверок и сводок."""
        if kind is None:
            rows = await self.db.fetch_all("SELECT * FROM jobs WHERE status = 'pending' ORDER BY run_at")
        else:
            rows = await self.db.fetch_all(
                "SELECT * FROM jobs WHERE status = 'pending' AND kind = ? ORDER BY run_at",
                (kind.value,),
            )
        return [Job(row) for row in rows]

    async def purge_finished(self, older_than: int) -> int:
        cursor = await self.db.execute(
            """
            DELETE FROM jobs
            WHERE status IN ('done', 'failed', 'cancelled')
              AND COALESCE(done_at, run_at) < ?
            """,
            (older_than,),
        )
        return cursor.rowcount

    async def get(self, dedup_key: str) -> Job | None:
        """Задача по ключу — в любом состоянии."""
        row = await self.db.fetch_one("SELECT * FROM jobs WHERE dedup_key = ?", (dedup_key,))
        return Job(row) if row is not None else None

    async def exists(self, dedup_key: str) -> bool:
        """Есть ли ещё невыполненная задача с таким ключом."""
        row = await self.db.fetch_one(
            "SELECT 1 FROM jobs WHERE dedup_key = ? AND status = 'pending' LIMIT 1",
            (dedup_key,),
        )
        return row is not None

    @staticmethod
    def reminder_key(booking_id: int) -> str:
        return f"reminder:{booking_id}"

    @staticmethod
    def digest_key(day: str, owner_user_id: int) -> str:
        return f"digest:{day}:{owner_user_id}"
