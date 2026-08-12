# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.12.13-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.32
ARG POLYBOT_SOURCE_CHECKPOINT

FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE} AS runtime

ARG POLYBOT_SOURCE_CHECKPOINT

COPY --from=uv /uv /uvx /bin/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    UV_PYTHON_DOWNLOADS=0 \
    MPLCONFIGDIR=/tmp/polybot-matplotlib \
    POLYBOT_IMAGE_CHECKPOINT=${POLYBOT_SOURCE_CHECKPOINT}

LABEL org.opencontainers.image.revision=${POLYBOT_SOURCE_CHECKPOINT}

RUN groupadd --gid 10001 polybot \
    && useradd --uid 10001 --gid polybot --no-create-home --home-dir /app polybot

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY . .
RUN test "${#POLYBOT_SOURCE_CHECKPOINT}" -eq 40 \
    && case "$POLYBOT_SOURCE_CHECKPOINT" in *[!0-9a-f]*) exit 2 ;; *) : ;; esac \
    && install -d -o polybot -g polybot -m 0750 \
        /app/data/development/images \
        /app/logs/development \
        /tmp/polybot-matplotlib

USER 10001:10001
STOPSIGNAL SIGINT

CMD ["python", "bot.py", "--skip_tasks"]
