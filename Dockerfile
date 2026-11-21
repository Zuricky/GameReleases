FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DB_PATH=/app/data/game_releases.db

WORKDIR /app

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home \
        --shell /usr/sbin/nologin app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . \
    && mkdir -p /app/data \
    && chown app:app /app/data

USER app

VOLUME ["/app/data"]

CMD ["python", "-m", "gamereleases"]
