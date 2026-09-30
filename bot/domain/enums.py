"""Перечисления доменной модели."""

from __future__ import annotations

from enum import StrEnum


class FieldType(StrEnum):
    """Тип поля, которое клиент заполняет при записи."""

    PHONE = "phone"
    FIRST_NAME = "name"
    LAST_NAME = "lastname"
    COMMENT = "comment"
    TEXT = "text"


FIELD_LABELS: dict[FieldType, str] = {
    FieldType.PHONE: "Номер телефона",
    FieldType.FIRST_NAME: "Имя",
    FieldType.LAST_NAME: "Фамилия",
    FieldType.COMMENT: "Комментарий",
    FieldType.TEXT: "Дополнительное поле",
}

#: Значение по умолчанию для нового подбота: без чего запись невозможна.
DEFAULT_FIELDS: tuple[tuple[FieldType, bool], ...] = (
    (FieldType.FIRST_NAME, True),
    (FieldType.LAST_NAME, False),
    (FieldType.PHONE, True),
    (FieldType.COMMENT, False),
)

#: Ограничение длины комментария и произвольного текста.
MAX_TEXT_LENGTH = 500


class BookingStatus(StrEnum):
    """Жизненный цикл записи."""

    ACTIVE = "active"
    CANCELLED = "cancelled"
    DONE = "done"
    NO_SHOW = "no_show"


class CancelSource(StrEnum):
    """Кто отменил запись: от этого зависит, кому уходит уведомление."""

    CLIENT = "client"
    OWNER = "owner"


class JobKind(StrEnum):
    """Тип фоновой задачи."""

    REMINDER = "reminder"
    DIGEST = "digest"


class JobStatus(StrEnum):
    """Состояние фоновой задачи в очереди."""

    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
