# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c
ARG POLYBOT_SOURCE_CHECKPOINT

FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE} AS runtime

ARG POLYBOT_SOURCE_CHECKPOINT
ARG POLYBOT_RUNTIME_UID=10001
ARG POLYBOT_RUNTIME_GID=10001

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

RUN groupadd --gid "${POLYBOT_RUNTIME_GID}" polybot \
    && useradd --uid "${POLYBOT_RUNTIME_UID}" --gid polybot \
        --no-create-home --home-dir /app polybot

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY --chown=${POLYBOT_RUNTIME_UID}:${POLYBOT_RUNTIME_GID} . .
RUN test "${#POLYBOT_SOURCE_CHECKPOINT}" -eq 40 \
    && case "$POLYBOT_SOURCE_CHECKPOINT" in *[!0-9a-f]*) exit 2 ;; *) : ;; esac \
    && install -d -o root -g root -m 0755 /usr/local/share/polybot \
    && printf '%s\n' "$POLYBOT_SOURCE_CHECKPOINT" \
        > /usr/local/share/polybot/image-checkpoint \
    && chown root:root /usr/local/share/polybot/image-checkpoint \
    && chmod 0444 /usr/local/share/polybot/image-checkpoint \
    && install -d -o polybot -g polybot -m 0750 \
        /app/data/development/images \
        /app/logs/development \
        /tmp/polybot-matplotlib

USER ${POLYBOT_RUNTIME_UID}:${POLYBOT_RUNTIME_GID}
STOPSIGNAL SIGINT

CMD ["python", "scripts/run_development_beta.py", "--skip_tasks"]
