"""Компактные коды для inline-кнопок.

MAX ограничивает payload кнопки, поэтому в кнопку кладём короткий код,
а всё контекстное состояние держим в сессии пользователя. Из-за этого
код кнопки всегда короткий и не зависит от названия подбота.

Грамматика: `буква:аргумент[:аргумент]`

    m:home|m:book|m:mine|m:panel|m:help   корневое меню
    s:<id>                                открыть подбот как клиент
    d:<id>:<offset>                       выбор дня (смещение от сегодня)
    t:<id>:<offset>:<HHMM>                выбор слота
    z:<unix>                              нажат занятый слот
    c:<booking_id>                        запрос отмены записи
    y:<booking_id>                        подтверждение отмены
    b:ok                                  подтверждение записи
    f:fin                                 завершить многострочное поле

    n:new                                 создать подбот
    a:<id>                                карточка своего подбота
    a:<id>:sch                            расписание
    a:<id>:fld                            поля формы
    a:<id>:rul                            правила записи
    a:<id>:bk                             записи подбота
    a:<id>:link                           ссылка для клиентов
    e:<id>:title|desc                     правка названия/описания
    q:<id>[:y]                            удалить подбот
    h:<id>:<wd>                           выбрать день недели
    p:<id>:<key>[:<значение>...]         изменить настройку раздела
    i:<id>:<wd>:<index>                   удалить рабочий интервал
    r:<id>:<wd>:<index>                   удалить перерыв
"""

from __future__ import annotations

MAX_CALLBACK_LENGTH = 40


class CallbackError(ValueError):
    """Некорректный callback-код."""


def _validate(code: str) -> str:
    if len(code) > MAX_CALLBACK_LENGTH:
        raise CallbackError(f"Callback-код длиннее {MAX_CALLBACK_LENGTH} символов: {code}")
    return code


def parse_callback(payload: str | None) -> list[str]:
    """Разбирает код кнопки на секцию и аргументы."""
    return (payload or "").split(":")


def to_int(value: str | None) -> int | None:
    """Мягкий разбор числа: мусор в payload не должен ронять обработчик."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# --- Корневое меню --------------------------------------------------------
MENU_HOME = "m:home"
MENU_BOOK = "m:book"
MENU_MINE = "m:mine"
MENU_PANEL = "m:panel"
MENU_HELP = "m:help"


# --- Клиент ---------------------------------------------------------------
BOOKING_CONFIRM = "b:ok"


def field_form_finish() -> str:
    """Завершить ввод многострочного поля."""
    return "f:fin"


def subbot_open(subbot_id: int) -> str:
    return _validate(f"s:{subbot_id}")


def day_pick(subbot_id: int, offset: int) -> str:
    return _validate(f"d:{subbot_id}:{offset}")


def slot_pick(subbot_id: int, offset: int, start_minute: int) -> str:
    """Кнопка свободного слота.

    Время кодируется минутами от полуночи, а не строкой «10:00»: двоеточие
    в коде разделяет аргументы, и «10:00» распалось бы на две части —
    обработчик прочитал бы 10 минут вместо 10 часов.
    """
    return _validate(f"t:{subbot_id}:{offset}:{start_minute}")


def slot_busy(starts_at: int) -> str:
    """Кнопка занятого слота: нажатие объясняет, что время занято."""
    return _validate(f"z:{starts_at}")


def booking_cancel(booking_id: int) -> str:
    return _validate(f"c:{booking_id}")


def booking_cancel_confirm(booking_id: int) -> str:
    return _validate(f"y:{booking_id}")


# --- Админка --------------------------------------------------------------
def subbot_create() -> str:
    return "n:new"


def subbot_card(subbot_id: int) -> str:
    return _validate(f"a:{subbot_id}")


#: Разделы настроек подбота.
SECTION_SCHEDULE = "sch"
SECTION_FIELDS = "fld"
SECTION_RULES = "rul"
SECTION_BOOKINGS = "bk"
SECTION_LINK = "link"


def subbot_section(subbot_id: int, section: str) -> str:
    return _validate(f"a:{subbot_id}:{section}")


def weekday_pick(subbot_id: int, weekday: int) -> str:
    """День недели открывается отдельной буквой `h:`.

    Префикс `a:` занят разделами карточки (`a:<id>:sch`), поэтому день
    недели нельзя кодировать как `a:<id>:day:<wd>` — разбор принял бы
    `day` за название раздела.
    """
    return _validate(f"h:{subbot_id}:{weekday}")


def profile_edit(subbot_id: int, what: str) -> str:
    """what: 'title' или 'desc'."""
    return _validate(f"e:{subbot_id}:{what}")


def subbot_delete(subbot_id: int) -> str:
    return _validate(f"q:{subbot_id}")


def subbot_delete_confirm(subbot_id: int) -> str:
    return _validate(f"q:{subbot_id}:y")


def param_set(subbot_id: int, section: str, *args: str) -> str:
    """Изменить настройку: p:<id>:<раздел>[:<арг>...].

    Раздел стоит первым аргументом после `p:<id>`, поэтому разделы
    (расписание, поля, правила) не конфликтуют: у каждого своя проверка
    по названию раздела, а разбор делает один обработчик.

    Аргументы передаются по отдельности и не должны содержать двоеточие:
    `param_set(1, "rul", "horizon", "14")` даёт `p:1:rul:horizon:14`,
    и обработчик читает `parts[3]` и `parts[4]`. Собирать «ключ:значение»
    одной строкой можно, но тогда разделение остаётся на волю разбора.
    """
    parts = ":".join([f"p:{subbot_id}", section, *args])
    return _validate(parts)


def interval_delete(subbot_id: int, weekday: int, index: int) -> str:
    return _validate(f"i:{subbot_id}:{weekday}:{index}")


def break_delete(subbot_id: int, weekday: int, index: int) -> str:
    return _validate(f"r:{subbot_id}:{weekday}:{index}")
