# JARVIS server image: one image, several roles (core, scheduler, admin commands).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# git: the memory engine keeps memory in a private git repo (ADR-010).
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY alembic.ini ./
COPY migrations ./migrations
COPY src ./src
RUN uv sync --locked --no-dev \
 && groupadd --system --gid 10001 jarvis \
 && useradd --system --uid 10001 --gid 10001 --no-create-home jarvis \
 && install -d -o 10001 -g 10001 -m 0700 /var/lib/jarvis/memory /backups/memory

USER jarvis
ENTRYPOINT ["jarvis-core"]
CMD ["serve"]
