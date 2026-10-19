# Two images from one Dockerfile (select with `target:` in docker-compose.yml):
#   bot - Telegram long-polling bot (default target, the last stage)
#   web - FastAPI admin panel (P5); shares the bot's service / data layer

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Run as an unprivileged user.
RUN useradd --system --uid 10001 --no-create-home bot


FROM base AS web

COPY requirements.txt requirements-web.txt ./
RUN pip install -r requirements-web.txt

COPY bot ./bot
COPY web ./web
COPY migrations ./migrations
COPY release ./release

# Release identity. These are the LAST layers on purpose: the values change on every release and
# must not invalidate the pip / apt layers above. VERSION.json is read by bot/version.py; the
# labels let the release script ask a running container which release it is (docker inspect).
ARG GIT_SHA=unknown
ARG APP_VERSION=dev
ARG BUILD_TIME=unknown
RUN printf '{"version":"%s","sha":"%s","built":"%s"}\n' "$APP_VERSION" "$GIT_SHA" "$BUILD_TIME" \
    > /app/VERSION.json
LABEL org.opencontainers.image.version="$APP_VERSION" \
      org.opencontainers.image.revision="$GIT_SHA" \
      org.opencontainers.image.created="$BUILD_TIME" \
      dating-bot.schema-tolerance="1"

USER bot
EXPOSE 8000

# Single worker on purpose: login sessions and the login throttle live in this process.
# No access log: the Telegram login callback carries a signed payload in its query string.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-m", "web.health"]

CMD ["python", "-m", "uvicorn", "web.app.main:create_app_from_env", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]


FROM base AS bot

# Tesseract (English only): reads payment screenshots for the admin's advisory check (P8). Without
# it the bot still works; every screenshot is then just marked "could not be read".
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot ./bot
COPY migrations ./migrations
COPY release ./release

# Release identity. These are the LAST layers on purpose: the values change on every release and
# must not invalidate the pip / apt layers above. VERSION.json is read by bot/version.py; the
# labels let the release script ask a running container which release it is (docker inspect).
ARG GIT_SHA=unknown
ARG APP_VERSION=dev
ARG BUILD_TIME=unknown
RUN printf '{"version":"%s","sha":"%s","built":"%s"}\n' "$APP_VERSION" "$GIT_SHA" "$BUILD_TIME" \
    > /app/VERSION.json
LABEL org.opencontainers.image.version="$APP_VERSION" \
      org.opencontainers.image.revision="$GIT_SHA" \
      org.opencontainers.image.created="$BUILD_TIME" \
      dating-bot.schema-tolerance="1"

USER bot

# The bot touches /tmp/bot.heartbeat every 15s; unhealthy if older than 60s.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "-m", "bot.health"]

CMD ["python", "-m", "bot.main"]
