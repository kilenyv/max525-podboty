"""Уведомления, напоминания, дайджесты и отмена записи.

Проверяются через тот же диспетчер, что и остальные сценарии. Важная
часть — что задача из очереди действительно выполнится — гоняется
вручную: в тесте планировщик не крутит фоновую петлю.
"""

from __future__ import annotations

import contextlib
import time
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from bot.db.repositories.jobs import Job
from bot.domain import callback as cb
from bot.domain.enums import CancelSource, JobKind
from bot.domain.models import Subbot
from bot.domain.timeutil import get_tz, localize, to_unix
from bot.services.booking import local_date
from bot.services.scheduler import schedule_reminder
from tests.conftest import CLIENT_ID, OWNER_ID, STRANGER_ID, slots_of

MOSCOW = ZoneInfo("Europe/Moscow")


async def _make_booking(harness, subbot: Subbot, starts_at: int, hours: int = 1):
    """Запись в указанный момент — без похода через клиентский сценарий."""
    return await harness.repos.bookings.create(
        subbot_id=subbot.id,
        user_id=CLIENT_ID,
        starts_at=starts_at,
        ends_at=starts_at + hours * 3600,
        timezone=subbot.timezone,
        field_values={},
    )


@contextlib.contextmanager
def digest_on(day: date):
    """Заставляет сводку считать «сегодняшним» заданный день.

    Сутки у подбота свои: полнонь в Екатеринбурге — это 19:00 UTC, а в
    Москве 21:00. Без такой подмены тест результат зависел бы от того,
    в какое время суток его запустили.
    """
    with patch("bot.services.notifier.local_date", return_value=day):
        yield


class TestMyBookings:
    async def test_empty_list_says_so(self, harness) -> None:
        screen = await harness.press(CLIENT_ID, cb.MENU_MINE)
        assert screen.has_text("нет") or screen.has_text("Нет")

    async def test_booking_is_listed(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.book(CLIENT_ID, subbot.id, offset=2)
        harness.sender.clear()

        screen = await harness.press(CLIENT_ID, cb.MENU_MINE)
        # Название подбота и время стоят на кнопке отмены, а не в тексте.
        assert screen.has_button(subbot.title)
        assert screen.has_button("10:00")

    async def test_list_shows_every_own_booking(self, harness) -> None:
        first = await harness.make_subbot(title="Маникюр")
        await harness.book(CLIENT_ID, first.id, offset=2)
        second = await harness.make_subbot(title="Брови")
        await harness.book(CLIENT_ID, second.id, offset=3)
        harness.sender.clear()

        screen = await harness.press(CLIENT_ID, cb.MENU_MINE)
        assert screen.has_button("Маникюр")
        assert screen.has_button("Брови")

    async def test_other_clients_bookings_are_hidden(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.book(CLIENT_ID, subbot.id)
        harness.sender.clear()

        screen = await harness.press(STRANGER_ID, cb.MENU_MINE)
        assert not screen.has_button("Анна")


class TestClientCancel:
    async def _book_later(self, harness, offset: int = 2, **rules):
        subbot = await harness.make_subbot(**rules)
        await harness.book(CLIENT_ID, subbot.id, offset=offset)
        booking = (await harness.repos.bookings.list_user_upcoming(CLIENT_ID))[0]
        harness.sender.clear()
        return subbot, booking

    async def test_cancel_is_confirmed_in_two_steps(self, harness) -> None:
        _subbot, booking = await self._book_later(harness)
        screen = await harness.press(CLIENT_ID, cb.booking_cancel(booking.id))

        assert cb.booking_cancel_confirm(booking.id) in screen.payloads()
        assert (await harness.repos.bookings.get_by_id(booking.id)).is_active

    async def test_confirmed_cancel_frees_the_slot(self, harness) -> None:
        subbot, booking = await self._book_later(harness)
        await harness.press(CLIENT_ID, cb.booking_cancel(booking.id))
        await harness.press(CLIENT_ID, cb.booking_cancel_confirm(booking.id))

        assert not (await harness.repos.bookings.get_by_id(booking.id)).is_active
        assert not await harness.repos.bookings.list_subbot_upcoming(subbot.id)

    async def test_slot_can_be_booked_again_after_cancel(self, harness) -> None:
        subbot, booking = await self._book_later(harness)
        await harness.press(CLIENT_ID, cb.booking_cancel(booking.id))
        await harness.press(CLIENT_ID, cb.booking_cancel_confirm(booking.id))

        screen = await harness.pick_day(STRANGER_ID, subbot.id, 2)
        assert slots_of(screen).taken == []

    async def test_owner_is_told_about_client_cancel(self, harness) -> None:
        _subbot, booking = await self._book_later(harness)
        await harness.press(CLIENT_ID, cb.booking_cancel(booking.id))
        await harness.press(CLIENT_ID, cb.booking_cancel_confirm(booking.id))

        assert harness.sender.has_text(OWNER_ID, "Клиент отменил")

    async def test_late_cancel_is_refused(self, harness) -> None:
        """Отменять можно не позже чем за cancel_notice_hours до записи."""
        # Запись завтра, а отменять разрешено за 48 часов: уже поздно.
        _subbot, booking = await self._book_later(harness, offset=1, cancel_notice_hours=48)
        screen = await harness.press(CLIENT_ID, cb.booking_cancel(booking.id))

        assert cb.booking_cancel_confirm(booking.id) not in screen.payloads()
        assert screen.has_text("48") or screen.has_text("позднее")
        assert (await harness.repos.bookings.get_by_id(booking.id)).is_active

    async def test_early_cancel_is_allowed(self, harness) -> None:
        _subbot, booking = await self._book_later(harness, offset=3, cancel_notice_hours=48)
        screen = await harness.press(CLIENT_ID, cb.booking_cancel(booking.id))

        assert cb.booking_cancel_confirm(booking.id) in screen.payloads()

    async def test_owner_may_cancel_late(self, harness) -> None:
        """Лимит по времени — только для клиента: мастеру он не мешает."""
        _subbot, booking = await self._book_later(harness, offset=1, cancel_notice_hours=48)
        screen = await harness.press(OWNER_ID, cb.booking_cancel(booking.id))

        assert cb.booking_cancel_confirm(booking.id) in screen.payloads()

    async def test_stranger_cannot_cancel(self, harness) -> None:
        _subbot, booking = await self._book_later(harness)
        screen = await harness.press(STRANGER_ID, cb.booking_cancel(booking.id))

        assert screen.has_text("не ваша запись")
        assert (await harness.repos.bookings.get_by_id(booking.id)).is_active

    async def test_second_cancel_says_it_is_already_cancelled(self, harness) -> None:
        _subbot, booking = await self._book_later(harness)
        await harness.press(CLIENT_ID, cb.booking_cancel(booking.id))
        await harness.press(CLIENT_ID, cb.booking_cancel_confirm(booking.id))
        harness.sender.clear()

        screen = await harness.press(CLIENT_ID, cb.booking_cancel(booking.id))
        assert screen.has_text("уже отменена")

    async def test_cancel_drops_the_reminder(self, harness) -> None:
        subbot = await harness.make_subbot(reminder_hours=24)
        await harness.book(CLIENT_ID, subbot.id, offset=3)
        booking = (await harness.repos.bookings.list_user_upcoming(CLIENT_ID))[0]
        key = harness.repos.jobs.reminder_key(booking.id)
        assert await harness.repos.jobs.exists(key)

        await harness.press(CLIENT_ID, cb.booking_cancel(booking.id))
        await harness.press(CLIENT_ID, cb.booking_cancel_confirm(booking.id))

        assert not await harness.repos.jobs.exists(key)


class TestBookingNotifications:
    async def test_owner_gets_the_booking_card(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.book(CLIENT_ID, subbot.id)

        assert harness.sender.has_text(OWNER_ID, "Новая запись")
        assert harness.sender.has_text(OWNER_ID, "Анна")
        assert harness.sender.has_text(OWNER_ID, "+7 (999) 123-45-67")

    async def test_owner_card_offers_cancel(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.book(CLIENT_ID, subbot.id)
        booking = (await harness.repos.bookings.list_user_upcoming(CLIENT_ID))[0]

        assert cb.booking_cancel(booking.id) in harness.sender.payloads_of(OWNER_ID)

    async def test_client_gets_a_confirmation_with_time(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.book(CLIENT_ID, subbot.id)

        assert harness.sender.has_text(CLIENT_ID, "Вы записаны")
        assert harness.sender.has_text(CLIENT_ID, subbot.title)
        assert harness.sender.has_text(CLIENT_ID, "10:00")

    async def test_stranger_hears_nothing(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.book(CLIENT_ID, subbot.id)

        assert not harness.sender.to(STRANGER_ID)


class TestReminder:
    async def _due_reminder(self, harness, offset: int = 3, **rules) -> Job:
        """Запись в будущем и её напоминание, уже готовое к отправке."""
        subbot = await harness.make_subbot(reminder_hours=rules.pop("reminder_hours", 24), **rules)
        await harness.book(CLIENT_ID, subbot.id, offset=offset)
        booking = (await harness.repos.bookings.list_user_upcoming(CLIENT_ID))[0]

        # Напоминание ставится на starts_at − reminder_hours; чтобы задача
        # стала пора, сдвигаем её в прошлое — как будто время пришло.
        await harness.repos.jobs.schedule(
            JobKind.REMINDER,
            harness.repos.jobs.reminder_key(booking.id),
            int(time.time()) - 1,
            {"booking_id": booking.id},
        )
        harness.sender.clear()
        return subbot, booking

    async def test_reminder_is_scheduled_on_creation(self, harness) -> None:
        subbot = await harness.make_subbot(reminder_hours=24)
        await harness.book(CLIENT_ID, subbot.id, offset=3)
        booking = (await harness.repos.bookings.list_user_upcoming(CLIENT_ID))[0]

        job = await harness.repos.jobs.get(harness.repos.jobs.reminder_key(booking.id))
        assert job is not None
        assert job.run_at == booking.starts_at - 24 * 3600

    async def test_no_reminder_when_the_lead_time_has_passed(self, harness) -> None:
        """Запись через два часа: 24-часовое напоминание уже позади."""
        subbot = await harness.make_subbot(reminder_hours=24)
        booking = await _make_booking(harness, subbot, harness.clock + 2 * 3600)

        await schedule_reminder(harness.repos, harness.notifier, subbot, booking)

        assert not await harness.repos.jobs.exists(harness.repos.jobs.reminder_key(booking.id))

    async def test_lead_time_follows_the_setting(self, harness) -> None:
        subbot = await harness.make_subbot(reminder_hours=6)
        booking = await _make_booking(harness, subbot, harness.clock + 48 * 3600)

        await schedule_reminder(harness.repos, harness.notifier, subbot, booking)

        job = await harness.repos.jobs.get(harness.repos.jobs.reminder_key(booking.id))
        assert job is not None
        assert job.run_at == booking.starts_at - 6 * 3600

    async def test_no_reminder_when_disabled(self, harness) -> None:
        subbot = await harness.make_subbot(reminder_hours=0)
        booking = await _make_booking(harness, subbot, harness.clock + 48 * 3600)

        await schedule_reminder(harness.repos, harness.notifier, subbot, booking)

        assert not await harness.repos.jobs.exists(harness.repos.jobs.reminder_key(booking.id))

    async def test_reminder_reaches_the_client(self, harness) -> None:
        subbot, _booking = await self._due_reminder(harness)

        await harness.scheduler.run_due_jobs()

        assert harness.sender.has_text(CLIENT_ID, "Напоминание о записи")
        assert harness.sender.has_text(CLIENT_ID, subbot.title)

    async def test_reminder_offers_a_cancellation(self, harness) -> None:
        _subbot, booking = await self._due_reminder(harness)

        await harness.scheduler.run_due_jobs()

        assert cb.booking_cancel(booking.id) in harness.sender.payloads_of(CLIENT_ID)

    async def test_reminder_job_is_marked_done(self, harness) -> None:
        _subbot, booking = await self._due_reminder(harness)
        key = harness.repos.jobs.reminder_key(booking.id)

        await harness.scheduler.run_due_jobs()

        assert not await harness.repos.jobs.exists(key)

    async def test_cancelled_booking_gets_no_reminder(self, harness) -> None:
        _subbot, booking = await self._due_reminder(harness)
        await harness.repos.bookings.cancel(booking.id, CancelSource.OWNER)
        harness.sender.clear()

        await harness.scheduler.run_due_jobs()

        assert not harness.sender.to(CLIENT_ID)


class TestDigest:
    async def test_digest_lists_todays_bookings(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.book(CLIENT_ID, subbot.id)
        harness.sender.clear()

        await harness.notifier.send_digest(OWNER_ID, subbot)

        assert harness.sender.has_text(OWNER_ID, "Анна")
        assert harness.sender.has_text(OWNER_ID, "10:00")

    async def test_empty_digest_names_the_subbot(self, harness) -> None:
        subbot = await harness.make_subbot()
        harness.sender.clear()

        await harness.notifier.send_digest(OWNER_ID, subbot)

        assert harness.sender.has_text(OWNER_ID, subbot.title)

    async def test_digest_offers_the_full_booking_list(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.book(CLIENT_ID, subbot.id)
        harness.sender.clear()

        await harness.notifier.send_digest(OWNER_ID, subbot)

        assert cb.subbot_section(subbot.id, cb.SECTION_BOOKINGS) in harness.sender.payloads_of(OWNER_ID)

    async def test_digests_are_planned_ahead(self, harness) -> None:
        await harness.make_subbot()
        await harness.scheduler.plan_digests()

        keys = {job.dedup_key for job in await harness.repos.jobs.pending()}
        assert any(key.startswith("digest:") for key in keys), keys

    async def test_planning_twice_does_not_duplicate_jobs(self, harness) -> None:
        await harness.make_subbot()
        await harness.scheduler.plan_digests()
        first = len(await harness.repos.jobs.pending())

        await harness.scheduler.plan_digests()

        assert len(await harness.repos.jobs.pending()) == first

    async def test_digest_runs_from_the_queue(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.book(CLIENT_ID, subbot.id)
        await harness.repos.jobs.schedule(
            JobKind.DIGEST,
            "digest:test",
            int(time.time()) - 1,
            {"subbot_id": subbot.id, "owner_user_id": OWNER_ID},
        )
        harness.sender.clear()

        await harness.scheduler.run_due_jobs()

        assert harness.sender.has_text(OWNER_ID, "Анна")

    async def test_digest_counts_the_subbot_timezone(self, harness) -> None:
        """Сутки подбота — екатеринбургские, а не серверные.

        Сводка за выбранный день должна увидеть запись в 00:00 местного
        времени, хотя в UTC это предыдущие сутки.
        """
        subbot = await harness.make_subbot(timezone="Asia/Yekaterinburg")
        tz = get_tz(subbot.timezone)
        day = harness.today() + timedelta(days=30)
        await _make_booking(harness, subbot, to_unix(localize(day, 0, tz)))
        harness.sender.clear()

        with digest_on(day):
            await harness.notifier.send_digest(OWNER_ID, subbot)

        assert "00:00" in harness.sender.last(OWNER_ID).text

    async def test_digest_skips_other_days(self, harness) -> None:
        subbot = await harness.make_subbot()
        tz = get_tz(subbot.timezone)
        day = harness.today() + timedelta(days=30)
        await _make_booking(harness, subbot, to_unix(localize(day + timedelta(days=1), 10, tz)))
        harness.sender.clear()

        with digest_on(day):
            await harness.notifier.send_digest(OWNER_ID, subbot)

        assert "10:00" not in harness.sender.last(OWNER_ID).text


class TestTimezones:
    def test_local_date_depends_on_the_timezone(self) -> None:
        """Один и тот же момент — разные сутки в разных поясах.

        Это самый частый источник ошибок в таком боте: запись «на сегодня»
        в одном поясе оказывается «на завтра» в соседнем.
        """
        moment = to_unix(datetime(2026, 3, 10, 22, 30, tzinfo=ZoneInfo("UTC")))

        assert local_date(moment, get_tz("Europe/Kaliningrad")) == date(2026, 3, 11)
        assert local_date(moment, get_tz("Europe/Moscow")) == date(2026, 3, 11)
        assert local_date(moment, ZoneInfo("UTC")) == date(2026, 3, 10)

    def test_midnight_comes_from_the_iana_base(self) -> None:
        """Смещение берётся из базы IANA, а не из константы «+3 часа».

        Берлин зимой UTC+1, летом UTC+2 — если бы бот держал одно
        смещение, полночь уезжала бы на час в половине года.
        """
        berlin = get_tz("Europe/Berlin")
        winter = to_unix(localize(date(2026, 1, 15), 0, berlin))
        summer = to_unix(localize(date(2026, 7, 15), 0, berlin))

        assert datetime.fromtimestamp(winter, tz=ZoneInfo("UTC")) == datetime(
            2026, 1, 14, 23, 0, tzinfo=ZoneInfo("UTC")
        )
        assert datetime.fromtimestamp(summer, tz=ZoneInfo("UTC")) == datetime(
            2026, 7, 14, 22, 0, tzinfo=ZoneInfo("UTC")
        )

    def test_unknown_timezone_falls_back_to_moscow(self) -> None:
        assert str(get_tz("Mars/Olympus")) == str(MOSCOW)


class TestMaintenance:
    async def test_past_booking_becomes_done(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.book(CLIENT_ID, subbot.id)
        booking = (await harness.repos.bookings.list_user_upcoming(CLIENT_ID))[0]
        await harness.db.execute(
            "UPDATE bookings SET starts_at = ?, ends_at = ? WHERE id = ?",
            (harness.clock - 7200, harness.clock - 3600, booking.id),
        )

        await harness.scheduler.maintenance()

        assert (await harness.repos.bookings.get_by_id(booking.id)).status == "done"

    async def test_cancelled_booking_is_not_moved_to_done(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.book(CLIENT_ID, subbot.id)
        booking = (await harness.repos.bookings.list_user_upcoming(CLIENT_ID))[0]
        await harness.repos.bookings.cancel(booking.id, CancelSource.CLIENT)
        await harness.db.execute(
            "UPDATE bookings SET starts_at = ?, ends_at = ? WHERE id = ?",
            (harness.clock - 7200, harness.clock - 3600, booking.id),
        )

        await harness.scheduler.maintenance()

        assert (await harness.repos.bookings.get_by_id(booking.id)).status == "cancelled"

    async def test_stale_session_is_dropped(self, harness) -> None:
        """Диалог, брошенный на сутки, не должен висять вечно."""
        session = await harness.ctx.session(CLIENT_ID)
        await session.set_state("client_field", subbot_id=1)
        await harness.db.execute("UPDATE sessions SET updated_at = ?", (harness.clock - 3 * 24 * 3600,))

        await harness.scheduler.maintenance()

        assert (await harness.ctx.session(CLIENT_ID)).state is None

    async def test_fresh_session_is_kept(self, harness) -> None:
        session = await harness.ctx.session(CLIENT_ID)
        await session.set_state("client_field", subbot_id=1)

        await harness.scheduler.maintenance()

        assert (await harness.ctx.session(CLIENT_ID)).state == "client_field"


class TestJobQueue:
    async def test_duplicate_key_does_not_double_schedule(self, harness) -> None:
        await harness.repos.jobs.schedule(JobKind.REMINDER, "k", harness.clock + 10, {"a": 1})
        await harness.repos.jobs.schedule(JobKind.REMINDER, "k", harness.clock + 20, {"a": 2})

        pending = await harness.repos.jobs.pending()
        assert len(pending) == 1
        # Повтор сдвигает время и обновляет полезную нагрузку.
        assert pending[0].run_at == harness.clock + 20
        assert pending[0].payload == {"a": 2}

    async def test_done_job_is_not_returned(self, harness) -> None:
        await harness.repos.jobs.schedule(JobKind.REMINDER, "k", harness.clock - 1, {})
        job = (await harness.repos.jobs.due())[0]

        await harness.repos.jobs.mark_done(job)

        assert not await harness.repos.jobs.due()
        assert not await harness.repos.jobs.pending()

    async def test_failed_job_is_retried(self, harness) -> None:
        await harness.repos.jobs.schedule(JobKind.REMINDER, "k", harness.clock - 1, {"a": 1})
        job: Job = (await harness.repos.jobs.due())[0]

        assert await harness.repos.jobs.mark_failed(job) is False
        # Задача отложена, а не выброшена: сетевой сбой не должен съесть её.
        assert await harness.repos.jobs.due() == []
        assert (await harness.repos.jobs.pending())[0].attempts == 1

    async def test_retry_is_postponed(self, harness) -> None:
        await harness.repos.jobs.schedule(JobKind.REMINDER, "k", harness.clock - 1, {"a": 1})
        job = (await harness.repos.jobs.due())[0]

        await harness.repos.jobs.mark_failed(job)

        assert (await harness.repos.jobs.pending())[0].run_at > harness.clock

    async def test_job_gives_up_after_three_attempts(self, harness) -> None:
        await harness.repos.jobs.schedule(JobKind.REMINDER, "k", harness.clock - 1, {"a": 1})
        for _ in range(3):
            job = (await harness.repos.jobs.due(now=harness.clock + 3600))[0]
            await harness.repos.jobs.mark_failed(job)

        assert not await harness.repos.jobs.due(now=harness.clock + 3600)
        assert not await harness.repos.jobs.pending()

    async def test_cancelled_job_is_not_run(self, harness) -> None:
        await harness.repos.jobs.schedule(JobKind.REMINDER, "k", harness.clock - 1, {"a": 1})

        await harness.repos.jobs.cancel("k")

        assert not await harness.repos.jobs.due()
        assert not await harness.repos.jobs.pending()

    async def test_scheduler_retries_a_job_it_could_not_handle(self, harness) -> None:
        """Задача без обработчика не должна крутиться вечно."""
        await harness.repos.jobs.schedule(JobKind.REMINDER, "k", harness.clock - 1, {"a": 1})

        for _ in range(3):
            await harness.scheduler.run_due_jobs(now=harness.clock + 3600)

        assert not await harness.repos.jobs.pending()
