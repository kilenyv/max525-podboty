"""Фоновые задачи: напоминания клиентам и утренние дайджесты владельцам.

Планировщик сделан на очередь в БД, а не на таймеры в памяти: при
перезапуске контейнера задачи не теряются. Отдельная задача на каждый
день не создаётся — планировщик сам досоздаёт дайджест на следующие дни.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import timedelta

from bot.db.connection import Database
from bot.db.repositories import Repositories
from bot.domain.enums import JobKind
from bot.domain.models import Booking, Subbot
from bot.domain.timeutil import get_tz, localize, to_unix
from bot.logging_config import get_logger
from bot.services.booking import BookingService, local_date
from bot.services.notifier import Notifier

log = get_logger(__name__)

#: На сколько суток вперёд планировщик ставит дайджесты.
DIGEST_DAYS_AHEAD = 2
#: Как далеко вперёд планируется очистка выполненных задач.
PURGE_AFTER_DAYS = 14


class Scheduler:
    """Фоновая петля: планирует, выполняет и прибирает очередь задач."""

    def __init__(
        self,
        db: Database,
        repos: Repositories,
        notifier: Notifier,
        booking_service: BookingService,
        poll_interval: int = 15,
    ) -> None:
        self.db = db
        self.repos = repos
        self.notifier = notifier
        self.booking = booking_service
        self.poll_interval = poll_interval
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    # --- Жизненный цикл ---------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._run(), name="scheduler")
            log.info("Планировщик фоновых задач запущен")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
            log.info("Планировщик остановлен")

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Сбой в цикле планировщика")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self.poll_interval)
            except TimeoutError:
                continue

    async def tick(self) -> None:
        """Один проход: спланировать, выполнить, прибраться."""
        await self.plan_digests()
        await self.run_due_jobs()
        await self.maintenance()

    # --- Планирование ------------------------------------------------------

    async def plan_digests(self) -> None:
        """Ставит дайджесты на ближайшие дни для всех активных подботов."""
        now = int(time.time())
        for subbot_id in await self.repos.subbots.list_active_ids():
            subbot = await self.repos.subbots.get_by_id(subbot_id)
            if subbot is None or subbot.digest_hour < 0:
                continue

            tz = subbot.tz
            today = local_date(now, tz)
            for offset in range(DIGEST_DAYS_AHEAD):
                day = today + timedelta(days=offset)
                run_at = to_unix(localize(day, subbot.digest_hour, tz))
                if run_at <= now:
                    continue
                await self.repos.jobs.schedule(
                    JobKind.DIGEST,
                    self.repos.jobs.digest_key(day.isoformat(), subbot.owner_user_id),
                    run_at,
                    {"subbot_id": subbot.id, "owner_user_id": subbot.owner_user_id},
                )

    # --- Выполнение --------------------------------------------------------

    async def run_due_jobs(self, now: int | None = None) -> None:
        for job in await self.repos.jobs.due(now):
            try:
                handled = await self._handle(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Ошибка выполнения задачи %s", job.dedup_key)
                handled = False

            if handled:
                await self.repos.jobs.mark_done(job)
            else:
                last = await self.repos.jobs.mark_failed(job)
                log.info("Задача %s не выполнена%s", job.dedup_key, ", попытки исчерпаны" if last else "")

    async def _handle(self, job) -> bool:  # noqa: ANN001 - Job
        if job.kind is JobKind.REMINDER:
            return await self.notifier.send_reminder(int(job.payload["booking_id"]))
        if job.kind is JobKind.DIGEST:
            subbot = await self.repos.subbots.get_by_id(int(job.payload["subbot_id"]))
            if subbot is None or not subbot.is_active:
                return True
            return await self.notifier.send_digest(subbot.owner_user_id, subbot)
        return True

    # --- Обслуживание ------------------------------------------------------

    async def maintenance(self) -> None:
        now = int(time.time())
        await self.booking.close_past_bookings()
        await self.repos.sessions.purge_stale()
        await self.repos.jobs.purge_finished(now - PURGE_AFTER_DAYS * 24 * 3600)


# --- Постановка задач из прикладного кода --------------------------------


async def schedule_reminder(
    repos: Repositories,
    notifier: Notifier,
    subbot: Subbot,
    booking: Booking,
) -> None:
    """Напоминание клиенту за reminder_hours до записи. 0 — не ставим."""
    hours = getattr(subbot, "reminder_hours", 0)
    if hours <= 0:
        return
    run_at = booking.starts_at - hours * 3600
    if run_at <= int(time.time()):
        return
    await repos.jobs.schedule(
        JobKind.REMINDER,
        repos.jobs.reminder_key(booking.id),
        run_at,
        {"booking_id": booking.id},
    )
    log.debug("Напоминание %s запланировано на %s", booking.id, run_at)


def local_day_bounds(now: int, timezone: str) -> tuple[int, int]:
    """Границы локальных суток в unix-секундах."""
    tz = get_tz(timezone)
    day = local_date(now, tz)
    return to_unix(localize(day, 0, tz)), to_unix(localize(day + timedelta(days=1), 0, tz))
