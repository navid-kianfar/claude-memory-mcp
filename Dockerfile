# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Stage 1: build the Vite + React frontend into static assets
# ---------------------------------------------------------------------------
FROM node:20-slim AS frontend

WORKDIR /build/frontend

# Install dependencies first for better layer caching.
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci

# Build the static bundle -> /build/frontend/dist
COPY frontend/ ./
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2: final runtime image with the Python MCP daemon
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS final

# uv reads these to create a project-local virtualenv at /app/.venv and to
# avoid copying packages (link-mode=copy keeps it self-contained).
ENV UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_COMPILE_BYTECODE=1 \
    PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1

# curl is needed for the HEALTHCHECK below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Install the uv package manager.
RUN pip install --no-cache-dir uv

WORKDIR /app

# Install Python dependencies first (cached unless dependency files change).
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
RUN uv sync --all-extras --frozen

# Bring in the pre-built frontend assets. The daemon resolves this path
# relative to the repo root, i.e. <repo>/frontend/dist.
COPY --from=frontend /build/frontend/dist ./frontend/dist

# Runtime configuration. DAEMON_HOST must be 0.0.0.0 so the port is reachable
# from outside the container.
ENV MEMORY_MCP_DATA_DIR=/data \
    MEMORY_MCP_DAEMON_HOST=0.0.0.0 \
    MEMORY_MCP_DAEMON_PORT=8765

# DuckDB data files persist here.
VOLUME /data

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8765/api/health || exit 1

# Run the HTTP daemon (serves both the MCP endpoint and the web UI/JSON API).
CMD ["memory-mcp", "serve"]
