FROM python:3.12-slim AS base
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /srv
ENV UV_PROJECT_ENVIRONMENT=/usr/local UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock ./

# Test stage: Chrysalis builds this target before every deploy; a failing test fails the build.
# Normal builds skip it (nothing below depends on it).
FROM base AS test
RUN uv sync --frozen --no-install-project
COPY app ./app
COPY migrations ./migrations
COPY tests ./tests
RUN DATABASE_URL=unused REDIS_URL=unused python -m pytest -q tests

FROM base AS runtime
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
COPY migrations ./migrations

EXPOSE 8000
# Graceful shutdown lets in-flight requests finish while the deploy script drains this copy.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "20"]
