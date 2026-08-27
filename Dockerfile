# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c

FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE} AS runtime

ARG POLYBOT_UID=1000
ARG POLYBOT_GID=1000

COPY --from=uv /uv /uvx /bin/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    UV_PYTHON_DOWNLOADS=0 \
    MPLCONFIGDIR=/tmp/polybot-matplotlib

RUN groupadd --gid "${POLYBOT_GID}" polybot \
    && useradd --uid "${POLYBOT_UID}" --gid polybot \
        --no-create-home --home-dir /app polybot

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY --chown=${POLYBOT_UID}:${POLYBOT_GID} . .
RUN install -d -o polybot -g polybot -m 0750 \
        /app/data/images \
        /app/logs \
        /tmp/polybot-matplotlib

USER ${POLYBOT_UID}:${POLYBOT_GID}
STOPSIGNAL SIGINT

CMD ["python", "bot.py"]
