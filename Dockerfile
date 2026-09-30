# Образ бота подботов.
#
# Сборка одноэтапная и без git намеренно: все зависимости приходят из
# PyPI с зафиксированными версиями, поэтому образ собирается за ~1 минуту
# (требование — не более 5) и воспроизводится из requirements.txt.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATABASE_PATH=/app/data/bot.db \
    TZ=Europe/Moscow

# tzdata: без неё ZoneInfo не находит Europe/Moscow и расписание подботов
# уезжает на час. Пакет tzdata из requirements.txt закрывает ZoneInfo,
# системный нужен остальным библиотекам и команде date.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Слой зависимостей отдельно от кода: правка бота не пересобирает pip.
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY bot ./bot

# Непривилегированный пользователь: токен бота лежит в переменных
# окружения этого процесса, и у него нет причин работать от root.
RUN mkdir -p /app/data \
    && useradd --create-home --uid 1000 botapp \
    && chown -R botapp:botapp /app
USER botapp

VOLUME ["/app/data"]

# HTTP-эндпоинта у бота нет: он работает через Long Polling и входящих
# соединений не принимает, порты наружу не пробрасываются. Проверять
# нечего, кроме доступности базы: том, примонтированный не туда, иначе
# тихо ломает каждую запись сообщением «нет такой таблицы».
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import os,sqlite3,sys; c=sqlite3.connect(os.environ.get('DATABASE_PATH','/app/data/bot.db')); sys.exit(0 if c.execute('SELECT name FROM sqlite_master WHERE name=?',('subbots',)).fetchone() else 1)"

CMD ["python", "-m", "bot"]
