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

# ── runtime: git, the sandbox dependencies and a Python test toolchain, non-root ─
# The repositories' tests run with their own toolchain, not ticket2pr's virtualenv: this image
# ships uv, e.g. `T2P_TEST_COMMAND="uv run --frozen pytest"`. Prefer toolchains and
# dependencies baked into a derived image over T2P_TEST_NETWORK=true (see docs/security.md).
FROM python:3.12-slim-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates bubblewrap socat \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /uvx /usr/local/bin/
RUN useradd --create-home --uid 10001 ticket2pr
# Root-owned: untrusted code (unsandboxed mode) must not plant a .env or patch the venv.
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    T2P_DATA_DIR=/data
RUN mkdir /data && chown ticket2pr:ticket2pr /data
USER ticket2pr
# Not /data: untrusted code can write there when the sandbox is off, and the working
# directory is where a `.env` would be read from on the next start.
WORKDIR /app
VOLUME ["/data"]
EXPOSE 8080
# No HEALTHCHECK: it would exec a process with every secret in its environment, readable from
# /proc by unsandboxed tests. Probe GET /healthz from outside (e.g. a Kubernetes httpGet probe).
ENTRYPOINT ["ticket2pr"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8080"]
