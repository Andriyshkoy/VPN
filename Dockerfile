FROM python:3.14-slim@sha256:b877e50bd90de10af8d82c57a022fc2e0dc731c5320d762a27986facfc3355c1 AS base

ARG APP_UID=10001
ARG APP_GID=10001

WORKDIR /app

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PYTHONPATH=/app

COPY requirements.txt .

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential gcc \
    && python -m pip install --upgrade pip==26.1.2 \
    && python -m pip install -r requirements.txt \
    && apt-get purge -y --auto-remove build-essential gcc \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid "${APP_GID}" app \
    && useradd --system --uid "${APP_UID}" --gid app --home-dir /app app

COPY --chown=app:app core ./core


FROM base AS admin
COPY --chown=app:app admin ./admin
USER app
CMD ["uvicorn", "admin.app:app", "--host=0.0.0.0", "--port=8000"]


FROM base AS bot
COPY --chown=app:app bot ./bot
USER app
CMD ["python", "-m", "bot.main"]


FROM base AS billing
COPY --chown=app:app billing_daemon ./billing_daemon
USER app
ENTRYPOINT ["/bin/sh", "-c"]


FROM base AS migrations
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app alembic.ini .
USER app
ENTRYPOINT ["alembic", "upgrade", "head"]


# Preserve the historical default build as a reusable backend runtime image.
FROM base AS runtime
USER app
