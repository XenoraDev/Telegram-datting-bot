FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot ./bot
COPY migrations ./migrations

# Run as an unprivileged user.
RUN useradd --system --uid 10001 --no-create-home bot
USER bot

# The bot touches /tmp/bot.heartbeat every 15s; unhealthy if older than 60s.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "-m", "bot.health"]

CMD ["python", "-m", "bot.main"]
