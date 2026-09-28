FROM python:3.12-slim AS base
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
# Docker CLI + compose/buildx plugins (static binaries) to drive the host's Docker.
COPY --from=docker:29-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker:29-cli /usr/local/libexec/docker/cli-plugins /usr/local/libexec/docker/cli-plugins
RUN apt-get update \
 && apt-get install -y --no-install-recommends git openssh-client \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
ENV UV_PROJECT_ENVIRONMENT=/usr/local UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock ./

FROM base AS test
RUN uv sync --frozen --no-install-project
COPY app ./app
COPY tests ./tests
RUN python -m pytest -q tests

FROM base AS runtime
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
# SSH (used by git) refuses to run for a user id with no account, so create one
# matching your server user; the ids come from .env via docker-compose.yml.
ARG AURELIA_UID=1000
ARG AURELIA_GID=1000
RUN (getent group "$AURELIA_GID" || groupadd -g "$AURELIA_GID" chrysalis) \
 && useradd -u "$AURELIA_UID" -g "$AURELIA_GID" -M -d /tmp -s /usr/sbin/nologin chrysalis
USER ${AURELIA_UID}:${AURELIA_GID}
EXPOSE 9100
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9100", "--app-dir", "/srv"]
