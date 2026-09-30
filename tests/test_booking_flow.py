"""Клиентский путь целиком: ссылка → день → слот → форма → запись.

Сценарий идёт через настоящий диспетчер, поэтому проверяется и разбор
callback-кодов, и порядок регистрации обработчиков.
"""

from __future__ import annotations

import pytest

from bot.db.repositories.bookings import SlotUnavailableError
from bot.domain import callback as cb
from bot.domain.deeplink import make_payload
from bot.domain.enums import FieldType
from tests.conftest import CLIENT_ID, OWNER_ID, slots_of

SECOND_CLIENT = 2005


async def make_slim(harness, *fields: FieldType):
    """Подбот с расписанием только на сегодня и ровно указанными полями.

    `create` заводит все поля по умолчанию, поэтому лишние выключаются
    явно — иначе форма спрашивала бы ещё и имя.
    """
    subbot = await harness.repos.subbots.create(
        owner_user_id=OWNER_ID, title="Маникюр", description="", timezone="Europe/Moscow"
    )
    await harness.repos.subbots.update_rules(subbot.id, OWNER_ID, slot_minutes=60)
    await harness.repos.subbots.add_interval(subbot.id, OWNER_ID, harness.weekday_today(), 600, 780)
    for field_type in FieldType:
        await harness.repos.subbots.set_field_enabled(
            subbot.id, OWNER_ID, field_type, enabled=field_type in fields, is_required=True
        )
    return await harness.reload(subbot.id)


class TestDeepLink:
    async def test_start_without_payload_shows_the_menu(self, harness) -> None:
        screen = await harness.start_bot(CLIENT_ID)
        assert screen.has_text("Записаться")
        assert cb.MENU_PANEL in screen.payloads()

    async def test_start_with_payload_opens_the_subbot(self, harness) -> None:
        subbot = await harness.make_subbot()
        screen = await harness.start_bot(CLIENT_ID, make_payload(subbot.slug))
        assert subbot.title in screen.text
        assert cb.subbot_open(subbot.id) in screen.payloads()

    async def test_unknown_payload_is_not_fatal(self, harness) -> None:
        screen = await harness.start_bot(CLIENT_ID, "s_no-such-subbot")
        assert screen.has_text("не найден") or screen.has_text("Проверьте адрес")

    async def test_garbage_payload_falls_back_to_the_menu(self, harness) -> None:
        # Пользователь может открыть бота и дописать что угодно после start=.
        screen = await harness.start_bot(CLIENT_ID, "помощь")
        assert cb.MENU_BOOK in screen.payloads()
        assert cb.MENU_PANEL in screen.payloads()


class TestPickDay:
    async def test_days_show_free_slot_counts(self, harness) -> None:
        subbot = await harness.make_subbot()
        screen = await harness.open_days(CLIENT_ID, subbot.id)

        days = [btn.text for btn in screen.buttons() if btn.payload and btn.payload.startswith("d:")]
        assert days, "нет ни одной кнопки выбора дня"
        # 10:00–13:00 при часовом слоте — ровно три слота в день.
        assert all("3" in label for label in days), days

    async def test_menu_button_is_reachable(self, harness) -> None:
        subbot = await harness.make_subbot()
        assert cb.MENU_HOME in (await harness.open_days(CLIENT_ID, subbot.id)).payloads()

    async def test_unconfigured_subbot_offers_no_days(self, harness) -> None:
        subbot = await harness.repos.subbots.create(
            owner_user_id=OWNER_ID, title="Пусто", description="", timezone="Europe/Moscow"
        )
        screen = await harness.open_days(CLIENT_ID, subbot.id)
        assert not [p for p in screen.payloads() if p.startswith("d:")]

    async def test_deleted_subbot_gives_a_clear_message(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.repos.subbots.delete(subbot.id, OWNER_ID)
        screen = await harness.open_days(CLIENT_ID, subbot.id)
        assert screen.has_text("не найден") or screen.has_text("Проверьте адрес")

    async def test_day_outside_the_horizon_is_refused(self, harness) -> None:
        subbot = await harness.make_subbot(horizon_days=3)
        screen = await harness.press(CLIENT_ID, cb.day_pick(subbot.id, 10))
        assert (
            screen.has_text("горизонт")
            or screen.has_text("устарела")
            or not [p for p in screen.payloads() if p.startswith("t:")]
        )


class TestPickSlot:
    async def test_slots_follow_the_interval(self, harness) -> None:
        subbot = await harness.make_subbot()
        assert slots_of(await harness.pick_first_day(CLIENT_ID, subbot.id)).free == [
            "10:00",
            "11:00",
            "12:00",
        ]

    async def test_break_removes_slots(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.repos.subbots.add_break(subbot.id, OWNER_ID, harness.weekday_today(), 660, 720)
        screen = await harness.pick_first_day(CLIENT_ID, subbot.id)
        # Перерыв 11:00–12:00 выбрасывает слот 11:00 целиком.
        assert "11:00" not in slots_of(screen).free

    async def test_booked_slot_is_visible_but_not_selectable(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.book(CLIENT_ID, subbot.id)

        screen = await harness.pick_first_day(SECOND_CLIENT, subbot.id)
        slots = slots_of(screen)

        assert slots.taken == ["· 10:00"], "занятый слот виден, но помечен точкой"
        assert slots.free == ["11:00", "12:00"]
        # У занятого слота отдельный код: нажатие не должно вести в форму.
        assert all(p.startswith("z:") for p in slots.taken_payloads)
        assert all(p.startswith("t:") for p in slots.free_payloads)

    async def test_pressing_a_busy_slot_explains_itself(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.book(CLIENT_ID, subbot.id)

        screen = await harness.pick_first_day(SECOND_CLIENT, subbot.id)
        busy = next(p for p in screen.payloads() if p.startswith("z:"))

        answer = await harness.press(SECOND_CLIENT, busy)
        assert answer.has_text("занял")

    async def test_fully_booked_subbot_says_so(self, harness) -> None:
        subbot = await harness.make_subbot(horizon_days=2)
        for offset in range(subbot.horizon_days):
            for slot in await harness.booking.day_slots(subbot, harness.day_at(offset)):
                await harness.repos.bookings.create(
                    subbot_id=subbot.id,
                    user_id=9000 + slot.starts_at % 1000,
                    starts_at=slot.starts_at,
                    ends_at=slot.ends_at,
                    timezone=subbot.timezone,
                    field_values={},
                )

        screen = await harness.open_days(CLIENT_ID, subbot.id)
        assert screen.has_text("занят") or screen.has_text("нет свободных")


class TestForm:
    async def test_required_fields_are_asked_in_order(self, harness) -> None:
        subbot = await harness.make_subbot()
        screen = await harness.pick_first_slot(CLIENT_ID, subbot.id)
        assert screen.has_text("Имя")

        answer = await harness.type(CLIENT_ID, "Анна")
        assert answer.has_text("Телефон") or answer.has_text("телефон")

    async def test_bad_phone_is_rejected_and_asked_again(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.pick_first_slot(CLIENT_ID, subbot.id)
        await harness.type(CLIENT_ID, "Анна")

        answer = await harness.type(CLIENT_ID, "не телефон")
        assert answer.has_text("номер")

    async def test_phone_is_normalized_in_the_confirmation(self, harness) -> None:
        subbot = await harness.make_subbot()
        screen = await harness.book(CLIENT_ID, subbot.id, answers=("Анна", "89991234567"))
        assert screen.has_text("+7 (999) 123-45-67")

    async def test_empty_required_field_is_rejected(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.pick_first_slot(CLIENT_ID, subbot.id)
        await harness.type(CLIENT_ID, "Анна")

        answer = await harness.type(CLIENT_ID, "   ")
        assert answer.has_text("обязательн")

    async def test_optional_field_is_not_asked(self, harness) -> None:
        subbot = await make_slim(harness, FieldType.PHONE, FieldType.COMMENT)
        await harness.repos.subbots.set_field_required(subbot.id, OWNER_ID, FieldType.COMMENT, False)
        subbot = await harness.reload(subbot.id)
        await harness.pick_first_slot(CLIENT_ID, subbot.id)

        screen = await harness.type(CLIENT_ID, "+7 999 123-45-67")
        assert cb.BOOKING_CONFIRM in screen.payloads(), "необязательный комментарий не спрашивают"

    async def test_comment_accumulates_until_done_button(self, harness) -> None:
        subbot = await make_slim(harness, FieldType.PHONE, FieldType.COMMENT)
        await harness.pick_first_slot(CLIENT_ID, subbot.id)
        await harness.type(CLIENT_ID, "+7 999 123-45-67")

        first = await harness.type(CLIENT_ID, "Аллергия на латекс")
        assert cb.field_form_finish() in first.payloads()

        await harness.type(CLIENT_ID, "И ещё короткая стрижка")
        screen = await harness.press(CLIENT_ID, cb.field_form_finish())

        assert cb.BOOKING_CONFIRM in screen.payloads()
        assert screen.has_text("Аллергия на латекс")
        assert screen.has_text("И ещё короткая стрижка")

    async def test_long_comment_is_capped(self, harness) -> None:
        subbot = await make_slim(harness, FieldType.COMMENT)
        await harness.pick_first_slot(CLIENT_ID, subbot.id)

        answer = await harness.type(CLIENT_ID, "я" * 900)
        assert answer.has_text("символ") or answer.has_text("сократите")

    async def test_deleted_subbot_mid_form_does_not_crash(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.pick_first_slot(CLIENT_ID, subbot.id)
        await harness.repos.subbots.delete(subbot.id, OWNER_ID)

        answer = await harness.type(CLIENT_ID, "Анна")
        assert answer.has_text("не существует") or answer.has_text("не найден")


class TestConfirmation:
    async def test_confirm_creates_the_booking(self, harness) -> None:
        subbot = await harness.make_subbot()
        screen = await harness.book(CLIENT_ID, subbot.id)

        assert screen.has_text("Вы записаны")
        bookings = await harness.repos.bookings.list_user_upcoming(CLIENT_ID)
        assert len(bookings) == 1
        assert bookings[0].subbot_id == subbot.id

    async def test_owner_is_notified_immediately(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.book(CLIENT_ID, subbot.id)

        assert harness.sender.has_text(OWNER_ID, "Анна")

    async def test_client_is_notified_separately(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.book(CLIENT_ID, subbot.id)
        # Ответ на кнопку и отдельное уведомление — два сообщения.
        assert len(harness.sender.to(CLIENT_ID)) >= 2

    async def test_owner_notification_has_cancel_button(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.book(CLIENT_ID, subbot.id)
        booking = (await harness.repos.bookings.list_user_upcoming(CLIENT_ID))[0]

        assert cb.booking_cancel(booking.id) in harness.sender.payloads_of(OWNER_ID)

    async def test_reminder_is_scheduled(self, harness) -> None:
        subbot = await harness.make_subbot(reminder_hours=24)
        # Напоминание за сутки имеет смысл только если запись не завтра:
        # на сегодняшний день 10:00 оно уже в прошлом и не ставится.
        await harness.book(CLIENT_ID, subbot.id, offset=3)
        booking = (await harness.repos.bookings.list_user_upcoming(CLIENT_ID))[0]

        assert await harness.repos.jobs.exists(harness.repos.jobs.reminder_key(booking.id))

    async def test_no_reminder_for_a_soon_booking(self, harness) -> None:
        # Запись через два часа: напоминать за сутки уже поздно.
        subbot = await harness.make_subbot(reminder_hours=24)
        await harness.book(CLIENT_ID, subbot.id)
        booking = (await harness.repos.bookings.list_user_upcoming(CLIENT_ID))[0]

        assert not await harness.repos.jobs.exists(harness.repos.jobs.reminder_key(booking.id))

    async def test_no_reminder_when_disabled(self, harness) -> None:
        subbot = await harness.make_subbot(reminder_hours=0)
        await harness.book(CLIENT_ID, subbot.id, offset=3)
        booking = (await harness.repos.bookings.list_user_upcoming(CLIENT_ID))[0]

        assert not await harness.repos.jobs.exists(harness.repos.jobs.reminder_key(booking.id))


class TestDoubleBooking:
    async def test_second_client_cannot_take_the_same_slot(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.book(CLIENT_ID, subbot.id)

        screen = await harness.pick_first_slot(SECOND_CLIENT, subbot.id)
        assert not screen.has_text("Записаться"), "клиент попал на занятый слот"

    async def test_service_refuses_a_race(self, harness) -> None:
        subbot = await harness.make_subbot()
        slot = (await harness.booking.day_slots(subbot, harness.day_at(0)))[0]

        first = await harness.booking.create_booking(
            subbot=subbot, user_id=1, starts_at=slot.starts_at, ends_at=slot.ends_at, values={}
        )
        assert first is not None

        with pytest.raises(SlotUnavailableError):
            await harness.booking.create_booking(
                subbot=subbot, user_id=2, starts_at=slot.starts_at, ends_at=slot.ends_at, values={}
            )

    async def test_same_user_cannot_double_book(self, harness) -> None:
        subbot = await harness.make_subbot()
        slot = (await harness.booking.day_slots(subbot, harness.day_at(0)))[0]
        await harness.booking.create_booking(
            subbot=subbot, user_id=1, starts_at=slot.starts_at, ends_at=slot.ends_at, values={}
        )

        with pytest.raises(SlotUnavailableError):
            await harness.booking.create_booking(
                subbot=subbot, user_id=1, starts_at=slot.starts_at, ends_at=slot.ends_at, values={}
            )


class TestTextOutsideDialogs:
    async def test_stray_text_points_back_to_the_menu(self, harness) -> None:
        assert (await harness.type(CLIENT_ID, "привет")).has_text("/start")

    async def test_commands_work_without_a_dialog(self, harness) -> None:
        assert (await harness.type(CLIENT_ID, "/help")).has_text("Как это работает")
        assert cb.MENU_BOOK in (await harness.type(CLIENT_ID, "/start")).payloads()

    async def test_unknown_command_is_treated_as_text(self, harness) -> None:
        assert (await harness.type(CLIENT_ID, "/nonsense")).has_text("/start")
