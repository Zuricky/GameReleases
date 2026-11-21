FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DB_PATH=/app/data/game_releases.db

WORKDIR /app

# UID/GID 1000 match the default user on common cloud VM images, so a
# bind-mounted ./data directory is writable without extra chown steps.
RUN groupadd --system --gid 1000 app \
    && useradd --system --uid 1000 --gid 1000 --no-create-home \
        --shell /usr/sbin/nologin app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . \
    && mkdir -p /app/data \
    && chown 1000:1000 /app/data

USER 1000:1000

VOLUME ["/app/data"]

CMD ["python", "-m", "gamereleases"]
