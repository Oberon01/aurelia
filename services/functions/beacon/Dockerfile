FROM python:3.12-slim AS base
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/
WORKDIR /srv
ENV UV_PROJECT_ENVIRONMENT=/usr/local UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock ./

# Chrysalis builds this stage before every deploy; failing tests stop the release.
FROM base AS test
RUN uv sync --frozen --no-install-project
COPY app ./app
COPY tests ./tests
RUN python -m pytest -q tests

FROM base AS runtime
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
EXPOSE 9000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9000", "--timeout-graceful-shutdown", "20", "--no-access-log"]
