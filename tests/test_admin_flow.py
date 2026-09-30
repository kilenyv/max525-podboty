"""Путь владельца: панель, создание подбота, расписание, поля, правила.

Проверяется ровно то, что делает мастер кнопками: каждый тест проходит
через настоящий диспетчер, поэтому заодно проверяется, что разделы
не мешают друг другу при разборе кодов.
"""

from __future__ import annotations

from bot.domain import callback as cb
from bot.domain.enums import FieldType
from bot.domain.timeutil import hhmm_to_minutes
from tests.conftest import CLIENT_ID, OWNER_ID, STRANGER_ID


async def _create_subbot(harness, title: str = "Маникюр", description: str = "Стрижка и маникюр"):
    """Полный диалог создания подбота, как это делает мастер."""
    await harness.press(OWNER_ID, cb.MENU_PANEL)
    await harness.press(OWNER_ID, cb.subbot_create())
    await harness.type(OWNER_ID, title)
    screen = await harness.type(OWNER_ID, description)
    assert screen.has_text("Настройте рабочее время")
    owned = await harness.repos.subbots.list_owned(OWNER_ID)
    assert owned, "подбот не создался"
    return owned[0]


class TestPanel:
    async def test_empty_panel_offers_creation(self, harness) -> None:
        screen = await harness.press(CLIENT_ID, cb.MENU_PANEL)
        assert cb.subbot_create() in screen.payloads()
        assert screen.has_text("0") or screen.has_text("нет")

    async def test_panel_lists_owned_subbots(self, harness) -> None:
        subbot = await harness.make_subbot()
        screen = await harness.press(OWNER_ID, cb.MENU_PANEL)

        assert cb.subbot_card(subbot.id) in screen.payloads()
        # Название подбота стоит на кнопке, а не в тексте.
        assert screen.has_button(subbot.title)

    async def test_stranger_sees_an_empty_panel(self, harness) -> None:
        """Чужой подбот не должен показываться по коду кнопки."""
        subbot = await harness.make_subbot()
        screen = await harness.press(STRANGER_ID, cb.MENU_PANEL)

        assert cb.subbot_card(subbot.id) not in screen.payloads()
        assert not screen.has_button(subbot.title)

    async def test_foreign_card_is_refused(self, harness) -> None:
        subbot = await harness.make_subbot()
        screen = await harness.press(STRANGER_ID, cb.subbot_card(subbot.id))

        assert screen.has_text("не ваш") or screen.has_text("не найден")
        assert not harness.sender.has_text(STRANGER_ID, "Ссылка для клиентов")

    async def test_limit_stops_new_subbots(self, harness) -> None:
        limit = harness.settings.max_subbots_per_user
        for index in range(limit):
            await harness.make_subbot(title=f"Услуга {index}")

        screen = await harness.press(OWNER_ID, cb.subbot_create())
        assert screen.has_text("Лимит")
        assert len(await harness.repos.subbots.list_owned(OWNER_ID)) == limit


class TestCreate:
    async def test_slug_comes_from_the_title(self, harness) -> None:
        subbot = await _create_subbot(harness, "Маникюр")
        assert subbot.slug == "manikyur"

    async def test_duplicate_title_gets_a_suffix(self, harness) -> None:
        first = await _create_subbot(harness, "Маникюр")
        second = await harness.make_subbot(title="Маникюр")

        assert first.slug == "manikyur"
        assert second.slug == "manikyur-2"

    async def test_description_can_be_skipped(self, harness) -> None:
        await harness.press(OWNER_ID, cb.MENU_PANEL)
        await harness.press(OWNER_ID, cb.subbot_create())
        await harness.type(OWNER_ID, "Стрижка")
        await harness.type(OWNER_ID, "-")

        subbot = (await harness.repos.subbots.list_owned(OWNER_ID))[0]
        assert subbot.description == ""

    async def test_link_is_shown_after_creation(self, harness) -> None:
        await harness.press(OWNER_ID, cb.MENU_PANEL)
        await harness.press(OWNER_ID, cb.subbot_create())
        await harness.type(OWNER_ID, "Маникюр")
        await harness.type(OWNER_ID, "Описание")

        assert harness.sender.has_text(OWNER_ID, "max.ru")

    async def test_new_subbot_has_no_schedule(self, harness) -> None:
        subbot = await _create_subbot(harness)
        assert not subbot.is_configured
        # А без расписания клиент не видится ни одного дня.
        day_codes = (await harness.open_days(CLIENT_ID, subbot.id)).payloads()
        assert not [code for code in day_codes if code.startswith("d:")]


class TestScheduleEditor:
    async def test_weekday_screen_asks_for_a_range(self, harness) -> None:
        subbot = await harness.make_bare_subbot()
        screen = await harness.press(OWNER_ID, cb.weekday_pick(subbot.id, 1))

        assert cb.param_set(subbot.id, "add_iv", "1") in screen.payloads()

    async def test_typed_range_is_saved(self, harness) -> None:
        subbot = await harness.make_bare_subbot()
        await harness.press(OWNER_ID, cb.weekday_pick(subbot.id, 1))
        await harness.press(OWNER_ID, cb.param_set(subbot.id, "add_iv", "1"))
        screen = await harness.type(OWNER_ID, "09:30-12:00")

        assert screen.has_text("09:30")
        reloaded = await harness.reload(subbot.id)
        assert reloaded.intervals.get(1)[0].start == 9 * 60 + 30

    async def test_two_intervals_per_day_are_allowed(self, harness) -> None:
        subbot = await harness.make_bare_subbot()
        await harness.press(OWNER_ID, cb.param_set(subbot.id, "add_iv", "2"))
        await harness.type(OWNER_ID, "18:00-20:00")
        await harness.press(OWNER_ID, cb.param_set(subbot.id, "add_iv", "2"))
        await harness.type(OWNER_ID, "09:00-11:00")

        reloaded = await harness.reload(subbot.id)
        assert len(reloaded.intervals.get(2)) == 2

    async def test_overlapping_interval_is_refused(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.press(OWNER_ID, cb.param_set(subbot.id, "add_iv", "2"))
        screen = await harness.type(OWNER_ID, "11:00-14:00")

        assert screen.has_text("пересекается")

    async def test_break_inside_an_interval_is_allowed(self, harness) -> None:
        """Перерыв по определению лежит внутри рабочего окна."""
        subbot = await harness.make_subbot()
        await harness.press(OWNER_ID, cb.param_set(subbot.id, "add_br", "3"))
        await harness.type(OWNER_ID, "11:00-12:00")

        reloaded = await harness.reload(subbot.id)
        assert len(reloaded.breaks.get(3)) == 1

    async def test_garbage_range_is_answered_with_an_example(self, harness) -> None:
        subbot = await harness.make_bare_subbot()
        await harness.press(OWNER_ID, cb.param_set(subbot.id, "add_iv", "4"))
        screen = await harness.type(OWNER_ID, "когда-нибудь")

        assert screen.has_text("10:00-18:00")

    async def test_shorter_than_slot_is_refused(self, harness) -> None:
        subbot = await harness.make_bare_subbot()
        await harness.press(OWNER_ID, cb.param_set(subbot.id, "add_iv", "5"))
        screen = await harness.type(OWNER_ID, "10:00-10:30")

        assert screen.has_text("короче") or screen.has_text("длительности")

    async def test_interval_can_be_deleted(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.press(OWNER_ID, cb.weekday_pick(subbot.id, 1))
        await harness.press(OWNER_ID, cb.interval_delete(subbot.id, 1, 0))

        assert not (await harness.reload(subbot.id)).intervals.get(1)

    async def test_break_can_be_deleted(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.repos.subbots.add_break(
            subbot.id, OWNER_ID, 4, hhmm_to_minutes("11:00"), hhmm_to_minutes("12:00")
        )
        await harness.press(OWNER_ID, cb.weekday_pick(subbot.id, 4))
        await harness.press(OWNER_ID, cb.break_delete(subbot.id, 4, 0))

        assert not (await harness.reload(subbot.id)).breaks.get(4)

    async def test_day_can_be_cleared(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.press(OWNER_ID, cb.weekday_pick(subbot.id, 2))
        await harness.press(OWNER_ID, cb.param_set(subbot.id, "clr", "2"))

        reloaded = await harness.reload(subbot.id)
        assert not reloaded.intervals.get(2)

    async def test_slot_length_preset_is_applied(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.press(OWNER_ID, cb.param_set(subbot.id, "slot", "30"))

        assert (await harness.reload(subbot.id)).slot_minutes == 30

    async def test_slot_length_must_divide_the_day(self, harness) -> None:
        subbot = await harness.make_subbot()
        screen = await harness.press(OWNER_ID, cb.param_set(subbot.id, "slot", "37"))

        assert screen.has_text("15, 30, 45, 60")
        assert (await harness.reload(subbot.id)).slot_minutes == 60

    async def test_manual_slot_length_is_typed(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.press(OWNER_ID, cb.param_set(subbot.id, "slot_manual"))
        await harness.type(OWNER_ID, "45")

        assert (await harness.reload(subbot.id)).slot_minutes == 45

    async def test_manual_slot_length_rejects_text(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.press(OWNER_ID, cb.param_set(subbot.id, "slot_manual"))
        screen = await harness.type(OWNER_ID, "час")

        assert screen.has_text("целое число")


class TestFieldsEditor:
    async def test_screen_shows_every_field_type(self, harness) -> None:
        subbot = await harness.make_subbot()
        screen = await harness.press(OWNER_ID, cb.subbot_section(subbot.id, cb.SECTION_FIELDS))

        for field_type in FieldType:
            # У обязательного поля код заканчивается на `off` (выключить),
            # у необязательного и выключенного — без него (сделать
            # обязательным либо включить заново).
            prefix = f"p:{subbot.id}:fld:{field_type.value}"
            assert any(p == prefix or p == f"{prefix}:off" for p in screen.payloads()), field_type

    async def test_optional_field_becomes_required_and_back(self, harness) -> None:
        subbot = await harness.make_subbot(fields=("name", "phone"))
        await harness.press(OWNER_ID, cb.param_set(subbot.id, "fld", "phone"))
        assert not (await harness.reload(subbot.id)).form_fields[-1].is_required

        await harness.press(OWNER_ID, cb.param_set(subbot.id, "fld", "phone"))
        reloaded = await harness.reload(subbot.id)
        assert all(item.is_required for item in reloaded.form_fields)

    async def test_field_can_be_switched_off(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.press(OWNER_ID, cb.param_set(subbot.id, "fld", "phone", "off"))

        types = {item.field_type for item in (await harness.reload(subbot.id)).form_fields}
        assert FieldType.PHONE not in types

    async def test_unknown_field_code_changes_nothing(self, harness) -> None:
        subbot = await harness.make_subbot()
        before = await harness.reload(subbot.id)
        await harness.press(OWNER_ID, cb.param_set(subbot.id, "fld", "nonsense"))

        after = await harness.reload(subbot.id)
        assert [item.field_type for item in after.form_fields] == [
            item.field_type for item in before.form_fields
        ]


class TestRulesEditor:
    async def test_horizon_preset_is_applied(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.press(OWNER_ID, cb.param_set(subbot.id, "rul", "horizon", "30"))

        assert (await harness.reload(subbot.id)).horizon_days == 30

    async def test_horizon_is_clamped(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.press(OWNER_ID, cb.param_set(subbot.id, "rul", "horizon", "9999"))

        assert (await harness.reload(subbot.id)).horizon_days == 365

    async def test_manual_horizon_is_typed(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.press(OWNER_ID, cb.param_set(subbot.id, "rul", "horizon"))
        await harness.type(OWNER_ID, "21")

        assert (await harness.reload(subbot.id)).horizon_days == 21

    async def test_out_of_range_manual_value_is_explained(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.press(OWNER_ID, cb.param_set(subbot.id, "rul", "notice"))
        screen = await harness.type(OWNER_ID, "9999")

        assert screen.has_text("0 до 720")

    async def test_timezone_button_is_applied(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.press(OWNER_ID, cb.param_set(subbot.id, "tz", "Asia/Yekaterinburg"))

        assert (await harness.reload(subbot.id)).timezone == "Asia/Yekaterinburg"

    async def test_unknown_timezone_is_refused(self, harness) -> None:
        subbot = await harness.make_subbot()
        screen = await harness.press(OWNER_ID, cb.param_set(subbot.id, "tz", "Mars/Olympus"))

        assert screen.has_text("часового пояса нет")
        assert (await harness.reload(subbot.id)).timezone == "Europe/Moscow"

    async def test_timezone_can_be_typed(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.press(OWNER_ID, cb.param_set(subbot.id, "tz_manual"))
        await harness.type(OWNER_ID, "Asia/Vladivostok")

        assert (await harness.reload(subbot.id)).timezone == "Asia/Vladivostok"

    async def test_reminder_can_be_switched_off(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.press(OWNER_ID, cb.param_set(subbot.id, "rul", "reminder", "0"))

        assert (await harness.reload(subbot.id)).reminder_hours == 0


class TestProfileEdit:
    async def test_title_is_renamed(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.press(OWNER_ID, cb.profile_edit(subbot.id, "title"))
        await harness.type(OWNER_ID, "Педикюр")

        assert (await harness.reload(subbot.id)).title == "Педикюр"

    async def test_long_title_is_trimmed(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.press(OWNER_ID, cb.profile_edit(subbot.id, "title"))
        await harness.type(OWNER_ID, "Я" * 200)

        assert len((await harness.reload(subbot.id)).title) <= 60

    async def test_stranger_cannot_rename(self, harness) -> None:
        subbot = await harness.make_subbot()
        screen = await harness.press(STRANGER_ID, cb.profile_edit(subbot.id, "title"))

        assert screen.has_text("не ваш") or screen.has_text("не найден")
        assert (await harness.reload(subbot.id)).title == "Маникюр"


class TestDelete:
    async def test_delete_asks_for_confirmation(self, harness) -> None:
        subbot = await harness.make_subbot()
        screen = await harness.press(OWNER_ID, cb.subbot_delete(subbot.id))

        assert cb.subbot_delete_confirm(subbot.id) in screen.payloads()
        assert await harness.repos.subbots.get_by_id(subbot.id) is not None

    async def test_confirmed_delete_removes_the_subbot(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.press(OWNER_ID, cb.subbot_delete_confirm(subbot.id))

        assert await harness.repos.subbots.get_by_id(subbot.id) is None

    async def test_delete_cancels_bookings_and_warns_clients(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.book(CLIENT_ID, subbot.id, offset=2)
        harness.sender.clear()

        await harness.press(OWNER_ID, cb.subbot_delete_confirm(subbot.id))

        assert harness.sender.has_text(CLIENT_ID, "отмен")
        assert not await harness.repos.bookings.list_subbot_upcoming(subbot.id)

    async def test_stranger_cannot_delete(self, harness) -> None:
        subbot = await harness.make_subbot()
        screen = await harness.press(STRANGER_ID, cb.subbot_delete_confirm(subbot.id))

        assert screen.has_text("не ваш") or screen.has_text("не найден")
        assert await harness.repos.subbots.get_by_id(subbot.id) is not None


class TestBookingsScreen:
    async def test_owner_sees_the_booking(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.book(CLIENT_ID, subbot.id)
        harness.sender.clear()

        screen = await harness.press(OWNER_ID, cb.subbot_section(subbot.id, cb.SECTION_BOOKINGS))
        booking = (await harness.repos.bookings.list_subbot_upcoming(subbot.id))[0]

        assert cb.booking_cancel(booking.id) in screen.payloads()
        assert screen.has_text("Анна")

    async def test_owner_cancels_and_client_is_told(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.book(CLIENT_ID, subbot.id)
        booking = (await harness.repos.bookings.list_subbot_upcoming(subbot.id))[0]
        harness.sender.clear()

        await harness.press(OWNER_ID, cb.booking_cancel(booking.id))
        await harness.press(OWNER_ID, cb.booking_cancel_confirm(booking.id))

        assert not await harness.repos.bookings.list_subbot_upcoming(subbot.id)
        assert harness.sender.has_text(CLIENT_ID, "отмен")
        assert harness.sender.has_text(OWNER_ID, "Вы отменили запись")

    async def test_stranger_sees_no_bookings(self, harness) -> None:
        subbot = await harness.make_subbot()
        await harness.book(CLIENT_ID, subbot.id)
        harness.sender.clear()

        screen = await harness.press(STRANGER_ID, cb.subbot_section(subbot.id, cb.SECTION_BOOKINGS))
        assert not screen.has_text("Анна")


class TestLinkScreen:
    async def test_link_is_copyable(self, harness) -> None:
        subbot = await harness.make_subbot()
        screen = await harness.press(OWNER_ID, cb.subbot_section(subbot.id, cb.SECTION_LINK))

        link = next(btn for btn in screen.buttons() if btn.copy)
        assert link.copy.endswith(subbot.slug)
