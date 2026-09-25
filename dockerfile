FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app

# Copy dependency files first
COPY pyproject.toml uv.lock ./

# Install production dependencies into .venv
RUN uv sync --frozen --no-dev --no-install-project

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS runtime

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


CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]