"""Админка: панель, карточка подбота, создание и удаление.

Коды кнопок админки короткие, поэтому один обработчик на префикс разбирает
код и передаёт его нужному разделу. Иначе несколько обработчиков
конкурировали бы за один и тот же код `p:`, а maxapi выполняет только
первый подошедший обработчик.

Порядок префиксов не важен: у всех свои фильтры. Важно лишь то, что этот
модуль не регистрирует `message_created` — весь ввод текста проходит
через единую точку входа в `bot.handlers.register_all`.
"""

from __future__ import annotations

from typing import Any

from maxapi.types import MessageCallback

from bot.domain import callback as cb
from bot.domain.deeplink import build_deep_link
from bot.domain.models import Subbot
from bot.filters import is_code, starts_with
from bot.handlers.admin import bookings as bookings_section
from bot.handlers.admin import fields as fields_section
from bot.handlers.admin import rules as rules_section
from bot.handlers.admin import schedule as schedule_section
from bot.handlers.base import (
    S_CREATE_DESC,
    S_CREATE_TITLE,
    AppContext,
    callback_payload,
    message_text,
    require_owned_subbot,
    user_id_of,
)
from bot.logging_config import get_logger
from bot.services import text
from bot.services.ui import Btn, Row

log = get_logger(__name__)

MAX_TITLE_LENGTH = 60
MAX_DESCRIPTION_LENGTH = 400


async def register_admin(dp, ctx: AppContext) -> None:  # noqa: ANN001
    """Регистрирует всю админку."""
    _register_menu(dp, ctx)
    _register_sections(dp, ctx)


# --- Корневое меню ----------------------------------------------------------


def _register_menu(dp, ctx: AppContext) -> None:  # noqa: ANN001

    @dp.message_callback(is_code(cb.MENU_PANEL))
    async def on_panel(event: MessageCallback) -> None:
        user_id = user_id_of(event)
        if user_id is None:
            return

        subbots = await ctx.repos.subbots.list_owned(user_id)
        if not subbots:
            await ctx.answer(event, text.panel_empty(), _panel_rows())
            return

        rows: list[Row] = [
            [
                Btn(
                    f"{'🟢' if subbot.is_configured else '🔴'} {subbot.title}",
                    payload=cb.subbot_card(subbot.id),
                )
            ]
            for subbot in subbots
        ]
        rows.append([Btn("➕ Создать подбот", payload=cb.subbot_create())])
        rows.append([Btn(text.BUTTON_MENU, payload=cb.MENU_HOME)])

        limit = ctx.settings.max_subbots_per_user
        header = f"{text.PANEL_ROOT}\n\nСтраниц записи: {len(subbots)} из {limit}."
        await ctx.answer(event, header, rows)

    @dp.message_callback(is_code(cb.subbot_create()))
    async def on_create_click(event: MessageCallback) -> None:
        user_id = user_id_of(event)
        if user_id is None:
            return

        limit = ctx.settings.max_subbots_per_user
        owned = await ctx.repos.subbots.count_owned(user_id)
        if owned >= limit:
            await ctx.answer(event, f"Лимит: не больше {limit} подботов на человека.", _panel_rows())
            return

        daily = ctx.settings.max_subbots_per_day
        created = await ctx.repos.subbots.count_created_today(user_id)
        if created >= daily:
            await ctx.answer(
                event, f"Сегодня уже создано {daily} подботов. Попробуйте завтра.", _panel_rows()
            )
            return

        session = await ctx.session(user_id)
        await session.set_state(S_CREATE_TITLE)
        await ctx.answer(
            event,
            "Как называется ваша услуга или канал?\n\n"
            "Например: «Маникюр», «Стрижка Анны», «Юрист Ирина».\n"
            "Из названия бот сделает ссылку для клиентов.",
        )


# --- Разделы подбота --------------------------------------------------------


def _register_sections(dp, ctx: AppContext) -> None:  # noqa: ANN001

    @dp.message_callback(starts_with("a:"))
    async def on_subbot(event: MessageCallback) -> None:
        """Карточка подбота и её разделы."""
        subbot, parts = await _resolve(ctx, event)
        if subbot is None:
            return

        section = parts[2] if len(parts) > 2 else ""
        show = {
            "": show_subbot_card,
            cb.SECTION_SCHEDULE: schedule_section.show_schedule_overview,
            cb.SECTION_FIELDS: fields_section.show_fields,
            cb.SECTION_RULES: rules_section.show_rules,
            cb.SECTION_BOOKINGS: bookings_section.show_subbot_bookings,
            cb.SECTION_LINK: show_link,
        }
        handler = show.get(section, show_subbot_card)
        await handler(ctx, event, subbot)

    @dp.message_callback(starts_with("h:"))
    async def on_weekday(event: MessageCallback) -> None:
        subbot, parts = await _resolve(ctx, event)
        if subbot is None or len(parts) < 3:
            return

        weekday = cb.to_int(parts[2])
        if weekday is None or not 1 <= weekday <= 7:
            return
        await schedule_section.show_day(ctx, event, subbot, weekday)

    @dp.message_callback(starts_with("p:"))
    async def on_param(event: MessageCallback) -> None:
        """Настройки разделов: p:<id>:<раздел>[:<ключ>[:<значение>]]."""
        subbot, parts = await _resolve(ctx, event)
        if subbot is None:
            return

        for section in (schedule_section, fields_section, rules_section):
            if await section.handle_param(ctx, event, subbot, parts):
                return

        log.debug("Неизвестный код настройки: %s", callback_payload(event))
        await show_subbot_card(ctx, event, subbot)

    @dp.message_callback(starts_with("i:", "r:"))
    async def on_delete_interval(event: MessageCallback) -> None:
        subbot, parts = await _resolve(ctx, event)
        if subbot is None:
            return
        await schedule_section.handle_interval_delete(ctx, event, subbot, parts)

    @dp.message_callback(starts_with("e:"))
    async def on_profile_edit(event: MessageCallback) -> None:
        """Запрашивает новое название или описание."""
        subbot, parts = await _resolve(ctx, event)
        if subbot is None or len(parts) < 3 or parts[2] not in ("title", "desc"):
            return

        user_id = subbot.owner_user_id
        session = await ctx.session(user_id)
        await session.set_state(
            S_CREATE_TITLE if parts[2] == "title" else S_CREATE_DESC,
            subbot_id=subbot.id,
        )

        prompt = (
            "Пришлите новое название:"
            if parts[2] == "title"
            else "Пришлите новое описание: что входит в услугу и где вы находитесь."
        )
        rows: list[Row] = [[Btn(text.BUTTON_CANCEL, payload=cb.subbot_card(subbot.id))]]
        await ctx.answer(event, prompt, rows)

    @dp.message_callback(starts_with("q:"))
    async def on_delete(event: MessageCallback) -> None:
        parts = cb.parse_callback(callback_payload(event))
        subbot, _ = await _resolve(ctx, event)
        if subbot is None:
            return

        if len(parts) > 2 and parts[2] == "y":
            await _delete_subbot(ctx, event, subbot)
            return

        active = await ctx.repos.subbots.count_active_bookings(subbot.id)
        body = (
            f"Удалить «{subbot.title}»?\n\n"
            "Страница записи и все её записи будут удалены без возможности восстановления. "
            f"Активных записей: {active}."
        )
        rows: list[Row] = [
            [Btn("Да, удалить", payload=cb.subbot_delete_confirm(subbot.id))],
            [Btn("Отмена", payload=cb.subbot_card(subbot.id))],
        ]
        await ctx.answer(event, body, rows)


# --- Создание и правка профиля (вызывается из единой точки ввода) -----------


async def handle_profile_text(ctx: AppContext, event: Any) -> bool:
    """Ввод названия и описания: при создании подбота и при правке.

    Возвращает False, если пользователь не в таком диалоге.
    """
    user_id = user_id_of(event)
    if user_id is None:
        return False

    session = await ctx.session(user_id)
    if session.state not in (S_CREATE_TITLE, S_CREATE_DESC):
        return False

    raw = message_text(event).strip()
    if not raw:
        return True

    editing_id = int(session.data.get("subbot_id") or 0)

    if session.state == S_CREATE_TITLE:
        if editing_id:
            await _save_profile(ctx, event, editing_id, user_id, title=raw)
            return True
        await session.set_state(S_CREATE_DESC, new_title=raw)
        await ctx.answer(
            event,
            "Отлично. Теперь коротко: что это за услуга и где вы находитесь?\n\n"
            "Это описание увидят клиенты на странице записи. Чтобы пропустить, отправьте «-».",
        )
        return True

    description = "" if raw == "-" else raw
    if editing_id:
        await _save_profile(ctx, event, editing_id, user_id, description=description)
        return True

    subbot = await ctx.repos.subbots.create(
        owner_user_id=user_id,
        title=str(session.data.get("new_title") or raw),
        description=description,
        timezone=ctx.settings.default_timezone,
    )
    await session.finish()

    link = build_deep_link(ctx.settings.bot_username, subbot.slug)
    await ctx.answer(event, text.subbot_admin_card(subbot, link, 0))
    await ctx.answer(
        event,
        "Настройте рабочее время — без него клиенты не смогут записаться.",
        [
            [
                Btn("🗓 Расписание", payload=cb.subbot_section(subbot.id, cb.SECTION_SCHEDULE)),
                Btn("🔗 Ссылка", payload=cb.subbot_section(subbot.id, cb.SECTION_LINK)),
            ],
            [Btn(text.BUTTON_PANEL, payload=cb.MENU_PANEL)],
        ],
    )
    return True


async def _save_profile(ctx: AppContext, event: Any, subbot_id: int, user_id: int, **changes: str) -> None:
    """Обрезает по длине, сохраняет и возвращает в карточку подбота."""
    if "title" in changes:
        changes["title"] = changes["title"][:MAX_TITLE_LENGTH]
    if "description" in changes:
        changes["description"] = changes["description"][:MAX_DESCRIPTION_LENGTH]

    await ctx.repos.subbots.update_profile(subbot_id, user_id, **changes)
    session = await ctx.session(user_id)
    await session.finish()

    subbot = await ctx.repos.subbots.get_owned(subbot_id, user_id)
    if subbot is not None:
        await show_subbot_card(ctx, event, subbot)


async def _delete_subbot(ctx: AppContext, event: MessageCallback, subbot: Subbot) -> None:
    """Удаляет подбот, отменив записи и предупредив клиентов."""
    cancelled = await ctx.repos.bookings.cancel_all_future(subbot.id)
    for booking in cancelled:
        # Гасим напоминание именно этой записи и предупреждаем клиента.
        await ctx.repos.jobs.cancel(ctx.repos.jobs.reminder_key(booking.id))
        await ctx.notifier.notify_client_of_cancel(subbot, booking)

    deleted = await ctx.repos.subbots.delete(subbot.id, subbot.owner_user_id)
    if not deleted:
        await ctx.answer(event, "Не удалось удалить подбот.")
        return

    tail = f"\nКлиентам отправлено уведомление об отмене: {len(cancelled)}." if cancelled else ""
    await ctx.answer(event, f"Подбот «{subbot.title}» удалён.{tail}", _back_to_panel())


# --- Экраны -----------------------------------------------------------------


async def show_subbot_card(ctx: AppContext, event: Any, subbot: Subbot) -> None:
    """Карточка подбота со всеми разделами настроек."""
    link = build_deep_link(ctx.settings.bot_username, subbot.slug)
    active = await ctx.repos.subbots.count_active_bookings(subbot.id)

    rows: list[Row] = [
        [
            Btn("🗓 Расписание", payload=cb.subbot_section(subbot.id, cb.SECTION_SCHEDULE)),
            Btn("⚙️ Правила", payload=cb.subbot_section(subbot.id, cb.SECTION_RULES)),
        ],
        [
            Btn("📝 Поля формы", payload=cb.subbot_section(subbot.id, cb.SECTION_FIELDS)),
            Btn("📋 Записи", payload=cb.subbot_section(subbot.id, cb.SECTION_BOOKINGS)),
        ],
        [Btn("🔗 Ссылка для клиентов", payload=cb.subbot_section(subbot.id, cb.SECTION_LINK))],
        [
            Btn("✏️ Название", payload=cb.profile_edit(subbot.id, "title")),
            Btn("📄 Описание", payload=cb.profile_edit(subbot.id, "desc")),
        ],
        [
            Btn("👁 Открыть как клиент", payload=cb.subbot_open(subbot.id)),
            Btn("🗑 Удалить", payload=cb.subbot_delete(subbot.id)),
        ],
        [Btn(text.BUTTON_PANEL, payload=cb.MENU_PANEL)],
    ]
    await ctx.answer(event, text.subbot_admin_card(subbot, link, active), rows)


async def show_link(ctx: AppContext, event: Any, subbot: Subbot) -> None:
    """Ссылка для клиентов: скопировать или открыть."""
    link = build_deep_link(ctx.settings.bot_username, subbot.slug)
    rows: list[Row] = [
        [Btn("Скопировать ссылку", copy=link)],
        [Btn("Открыть ссылку", url=link)],
        [Btn(text.BUTTON_BACK, payload=cb.subbot_card(subbot.id))],
    ]
    await ctx.answer(event, f"Ссылка на «{subbot.title}»:\n\n<code>{link}</code>", rows)


# --- Разбор кодов -----------------------------------------------------------


async def _resolve(ctx: AppContext, event: Any) -> tuple[Subbot | None, list[str]]:
    """Достаёт подбот из кода кнопки и проверяет, что он принадлежит пользователю."""
    user_id = user_id_of(event)
    parts = cb.parse_callback(callback_payload(event))
    if user_id is None or len(parts) < 2:
        return None, parts

    subbot_id = cb.to_int(parts[1])
    if subbot_id is None:
        return None, parts

    subbot = await require_owned_subbot(ctx, subbot_id, user_id)
    if subbot is None:
        await ctx.answer(event, "Подбот не найден или он не ваш.", _back_to_panel())
        return None, parts

    return subbot, parts


def _panel_rows() -> list[Row]:
    return [
        [Btn("➕ Создать подбот", payload=cb.subbot_create())],
        [Btn(text.BUTTON_MENU, payload=cb.MENU_HOME)],
    ]


def _back_to_panel() -> list[Row]:
    return [[Btn(text.BUTTON_PANEL, payload=cb.MENU_PANEL)]]


__all__ = ["handle_profile_text", "register_admin", "show_link", "show_subbot_card"]
