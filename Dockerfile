# syntax=docker/dockerfile:1.7

# ── build: resolve dependencies into a virtualenv ─────────────────────
FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev

# ── runtime: git + the Claude Code sandbox dependencies, non-root ─────
FROM python:3.12-slim-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates bubblewrap socat \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 10001 ticket2pr
COPY --from=build --chown=ticket2pr:ticket2pr /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    T2P_DATA_DIR=/data
RUN mkdir /data && chown ticket2pr:ticket2pr /data
USER ticket2pr
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"
ENTRYPOINT ["ticket2pr"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8080"]
