-- 001_init.sql — начальная схема.

-- Время везде хранится в unix-секундах UTC. Локальное время подбота
-- восстанавливается по колонке timezone, поэтому смена часового пояса
-- не сдвигает уже созданные записи.

CREATE TABLE IF NOT EXISTS users (
    user_id    INTEGER PRIMARY KEY,
    username   TEXT,
    first_name TEXT,
    last_name  TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS subbots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id       INTEGER NOT NULL,
    slug                TEXT    NOT NULL UNIQUE,
    title               TEXT    NOT NULL,
    description         TEXT    NOT NULL DEFAULT '',
    timezone            TEXT    NOT NULL DEFAULT 'Europe/Moscow',
    slot_minutes        INTEGER NOT NULL DEFAULT 30,
    horizon_days        INTEGER NOT NULL DEFAULT 14,
    cancel_notice_hours INTEGER NOT NULL DEFAULT 2,
    reminder_hours      INTEGER NOT NULL DEFAULT 24,
    digest_hour         INTEGER NOT NULL DEFAULT 9,
    is_active           INTEGER NOT NULL DEFAULT 1,
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_subbots_owner ON subbots (owner_user_id);

-- Дни недели нумеруются по ISO: 1 = понедельник, 7 = воскресенье.
-- Границы интервалов и перерывов — минуты от полуночи локального дня.
CREATE TABLE IF NOT EXISTS work_intervals (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    subbot_id INTEGER NOT NULL REFERENCES subbots (id) ON DELETE CASCADE,
    weekday   INTEGER NOT NULL,
    start_min INTEGER NOT NULL,
    end_min   INTEGER NOT NULL,
    position  INTEGER NOT NULL DEFAULT 0,
    CHECK (start_min >= 0 AND end_min <= 1440 AND start_min < end_min)
);

CREATE INDEX IF NOT EXISTS idx_intervals_subbot ON work_intervals (subbot_id, weekday, start_min);

CREATE TABLE IF NOT EXISTS breaks (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    subbot_id INTEGER NOT NULL REFERENCES subbots (id) ON DELETE CASCADE,
    weekday   INTEGER NOT NULL,
    start_min INTEGER NOT NULL,
    end_min   INTEGER NOT NULL,
    CHECK (start_min >= 0 AND end_min <= 1440 AND start_min < end_min)
);

CREATE INDEX IF NOT EXISTS idx_breaks_subbot ON breaks (subbot_id, weekday, start_min);

CREATE TABLE IF NOT EXISTS form_fields (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    subbot_id   INTEGER NOT NULL REFERENCES subbots (id) ON DELETE CASCADE,
    field_type  TEXT    NOT NULL,
    label       TEXT    NOT NULL,
    is_required INTEGER NOT NULL DEFAULT 1,
    position    INTEGER NOT NULL DEFAULT 0,
    max_length  INTEGER NOT NULL DEFAULT 255
);

CREATE INDEX IF NOT EXISTS idx_fields_subbot ON form_fields (subbot_id, position);

CREATE TABLE IF NOT EXISTS bookings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    subbot_id     INTEGER NOT NULL REFERENCES subbots (id) ON DELETE CASCADE,
    user_id       INTEGER NOT NULL,
    starts_at     INTEGER NOT NULL,
    ends_at       INTEGER NOT NULL,
    timezone      TEXT    NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'active',
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL,
    cancelled_at  INTEGER,
    cancel_source TEXT
);

CREATE INDEX IF NOT EXISTS idx_bookings_subbot_time
    ON bookings (subbot_id, starts_at, ends_at, status);
CREATE INDEX IF NOT EXISTS idx_bookings_user ON bookings (user_id, starts_at);

-- Страховка от двойной брони одного и того же слота.
-- Полное отсутствие пересечений проверяется в транзакции (BEGIN IMMEDIATE),
-- здесь ловим самый частый случай — ровно одно время старта.
CREATE UNIQUE INDEX IF NOT EXISTS uq_bookings_active_slot
    ON bookings (subbot_id, starts_at) WHERE status = 'active';

-- Значения полей хранятся EAV: набор полей у каждого подбота свой,
-- поэтому фиксированной схемы быть не может.
CREATE TABLE IF NOT EXISTS booking_values (
    booking_id INTEGER NOT NULL REFERENCES bookings (id) ON DELETE CASCADE,
    field_id   INTEGER NOT NULL REFERENCES form_fields (id) ON DELETE CASCADE,
    value      TEXT NOT NULL,
    PRIMARY KEY (booking_id, field_id)
);

-- Диалоги живут в БД, а не в памяти: перезапуск контейнера не обнуляет
-- шаг ввода данных, который пользователь не закончил.
CREATE TABLE IF NOT EXISTS sessions (
    user_id    INTEGER PRIMARY KEY,
    state      TEXT,
    data       TEXT    NOT NULL DEFAULT '{}',
    updated_at INTEGER NOT NULL
);

-- Фоновые задачи: напоминания клиентам и утренние дайджесты владельцам.
-- Очередь в БД, а не в памяти, — переживает рестарт и не теряет задачи.
CREATE TABLE IF NOT EXISTS jobs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT    NOT NULL,
    dedup_key  TEXT    NOT NULL UNIQUE,
    run_at     INTEGER NOT NULL,
    payload    TEXT    NOT NULL DEFAULT '{}',
    status     TEXT    NOT NULL DEFAULT 'pending',
    attempts   INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    done_at    INTEGER
);

CREATE INDEX IF NOT EXISTS idx_jobs_due ON jobs (status, run_at);

-- Мелкое хранилище служебных значений: маркер обновлений, счётчики.
CREATE TABLE IF NOT EXISTS bot_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
