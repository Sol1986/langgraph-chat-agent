FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app

# Copy dependency files first
COPY pyproject.toml uv.lock ./

# Install production dependencies into .venv
RUN uv sync --frozen --no-dev --no-install-project

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS runtime

RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the virtual environment created by uv
COPY --from=builder /app/.venv /app/.venv

# Copy application code
COPY app ./app

# Make the synced virtualenv's binaries (uvicorn, etc.) the default on PATH
ENV PATH="/app/.venv/bin:$PATH"

# CRITERIA: "Avoid running containers as root user"
RUN useradd --system --no-create-home --uid 10001 appuser
USER appuser

# Port is configurable at runtime (docker run -e PORT=9000 ...); defaults to 8000
ENV PORT=8000
EXPOSE 8000

# Shell form so ${PORT} expands; exec keeps uvicorn as PID 1 so it gets stop signals
# Adding exec right before uvicorn tells the hidden shell: "Initialize the variable, and then immediately destroy yourself and let Uvicorn take over your exact position as PID 1
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}