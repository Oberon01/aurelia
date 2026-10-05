FROM python:3.12-slim AS base
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
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
# Node (npx) for local MCP servers from npm; uvx (above) for ones from PyPI. They run as nobody (setpriv,
# part of util-linux in the base image), each as its own user with its own home under /tmp/lagrange.
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-bookworm-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
# iptables: local servers (their own users) are fenced off from my stack's network and from Lagrange itself.
RUN apt-get update && apt-get install -y --no-install-recommends iptables && rm -rf /var/lib/apt/lists/* \
 && ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && mkdir -p /tmp/lagrange && chmod 0711 /tmp/lagrange
COPY app ./app
EXPOSE 9000
# No access log: a webhook's address holds its secret.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9000", "--timeout-graceful-shutdown", "20", "--no-access-log"]
