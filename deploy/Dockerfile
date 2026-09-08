FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    GEMINI_COOKIE_PATH=/data/cookies \
    TMPDIR=/data/tmp

RUN groupadd --system --gid 10001 bot \
    && useradd --system --uid 10001 --gid bot --home-dir /app --shell /usr/sbin/nologin bot

WORKDIR /app

# Copy only packaging metadata and application code. Runtime secrets, .env,
# cookies, databases, and temporary media never enter an image layer.
COPY pyproject.toml ./
COPY src ./src

RUN python -m pip install --no-cache-dir . \
    && install -d -o bot -g bot -m 0700 /data/cookies \
    && install -d -o bot -g bot -m 0750 /data/db /data/tmp \
    && su -s /bin/sh bot -c \
        'test -w /data/cookies && test -w /data/db && test -w /data/tmp'

# The application stores its SQLite database at the relative path data/db.
# Running from / makes that resolve to the persistent /data/db volume.
WORKDIR /

USER bot:bot

STOPSIGNAL SIGTERM

CMD ["python", "-m", "gemini_tg_bot"]
