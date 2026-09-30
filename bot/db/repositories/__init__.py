"""Репозитории — единственное место, где живут SQL-запросы.

Обработчики к БД не ходят напрямую: так проще менять схему и проще
тестировать доменный слой.
"""

from __future__ import annotations

from bot.db.repositories.bookings import BookingsRepository
from bot.db.repositories.jobs import JobsRepository
from bot.db.repositories.sessions import SessionsRepository
from bot.db.repositories.subbots import SubbotsRepository
from bot.db.repositories.users import UsersRepository

__all__ = [
    "BookingsRepository",
    "JobsRepository",
    "Repositories",
    "SessionsRepository",
    "SubbotsRepository",
    "UsersRepository",
]


class Repositories:
    """Контейнер репозиториев. Создаётся один раз на приложение."""

    def __init__(self, db) -> None:  # noqa: ANN001 - circular-friendly
        self.db = db
        self.users = UsersRepository(db)
        self.subbots = SubbotsRepository(db)
        self.bookings = BookingsRepository(db)
        self.sessions = SessionsRepository(db)
        self.jobs = JobsRepository(db)
