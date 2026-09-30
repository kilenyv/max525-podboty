"""Клиентские обработчики: запись, отмена, свои записи."""

from __future__ import annotations

from bot.handlers.client.booking import handle_form_input, register_client_booking
from bot.handlers.client.my_bookings import register_my_bookings

__all__ = ["handle_form_input", "register_client_booking", "register_my_bookings"]
