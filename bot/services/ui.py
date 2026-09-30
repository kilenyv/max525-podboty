"""Сборка клавиатур.

Один способ описать кнопки для всех обработчиков: доменный код отдаёт
список кнопок, а этот модуль превращает его в вложение для maxapi.
Так прикладная логика не зависит от библиотеки MAX.
"""

from __future__ import annotations

from dataclasses import dataclass

from maxapi.types.attachments.attachment import Attachment
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder


@dataclass(frozen=True, slots=True)
class Btn:
    """Кнопка: либо действие (payload), либо ссылка, либо копирование."""

    text: str
    payload: str | None = None
    url: str | None = None
    copy: str | None = None

    def __post_init__(self) -> None:
        if not (self.payload or self.url or self.copy):
            raise ValueError("Кнопка без действия: нужен payload, url или copy")


Row = list[Btn]


def build(rows: list[Row] | None) -> list[Attachment] | None:
    """Превращает строки кнопок в attachments для отправки сообщения."""
    from maxapi.types.attachments.buttons import CallbackButton, ClipboardButton, LinkButton

    if not rows:
        return None

    for row in rows:
        # Ошибка `rows.append(wrap(...))` вместо `rows.extend(wrap(...))`
        # даёт ряд из рядов: maxapi принял бы это молча, а кнопки потерялись.
        if not isinstance(row, list) or any(isinstance(item, list) for item in row):
            raise TypeError(f"Ряд кнопок вложен в другой ряд: {row!r}")

    builder = InlineKeyboardBuilder()
    builder.payload = [[]]  # убираем пустой стартовый ряд конструктора

    for row in rows:
        buttons = []
        for item in row:
            if item.payload is not None:
                buttons.append(CallbackButton(text=item.text, payload=item.payload))
            elif item.url is not None:
                buttons.append(LinkButton(text=item.text, url=item.url))
            elif item.copy is not None:
                buttons.append(ClipboardButton(text=item.text, payload=item.copy))
        if buttons:
            builder.payload.append(buttons)

    if not builder.payload:
        return None
    return [builder.as_markup()]


def wrap(items: list[Btn], per_row: int = 2) -> list[Row]:
    """Раскладывает кнопки в ряды по несколько штук."""
    return [items[index : index + per_row] for index in range(0, len(items), per_row)]
