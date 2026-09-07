# One image is the whole instance: the packaged server, the Slidev toolchain it
# spawns, and the Chromium that toolchain exports a PDF with. Node is the base
# because the toolchain's version is the narrow one (frontend/.nvmrc); uv brings
# the Python that .python-version names.
FROM node:24.20.0-trixie-slim

# git mirrors a deck source and ssh reaches a private one (ADR 0010).
RUN apt-get update \
    && apt-get install --yes --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /usr/local/bin/uv

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1 \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

# The instance runs as its own user, and the directories it keeps state in are
# made here so that an empty volume mounted over one belongs to that user.
RUN useradd --create-home presentator \
    && mkdir -p /data/database /data/mirrors /data/builds \
    && chown presentator /data/database /data/mirrors /data/builds
RUN corepack enable pnpm

# The toolchain is installed as that same user, because a build writes its
# caches beside the project and Playwright keeps its browser under that user's
# home — the only two places the environment a build is given (PATH and HOME)
# can still name.
COPY --chown=presentator frontend/package.json frontend/pnpm-lock.yaml ./frontend/
USER presentator
WORKDIR /app/frontend
RUN corepack install && pnpm install --frozen-lockfile
USER root
RUN pnpm exec playwright install-deps chromium \
    && rm -rf /var/lib/apt/lists/*
USER presentator
RUN pnpm exec playwright install chromium
COPY --chown=presentator frontend ./

# Where this filesystem keeps what has to survive the container, and the address
# it answers at inside it; compose gives each directory a volume and offers the
# port to the machine alone.
ENV PRESENTATOR_DATABASE=/data/database/presentator.sqlite3 \
    PRESENTATOR_MIRRORS=/data/mirrors \
    PRESENTATOR_BUILDS=/data/builds \
    PRESENTATOR_TOOLCHAIN=/app/frontend \
    PRESENTATOR_HOST=0.0.0.0
WORKDIR /app
EXPOSE 8000
CMD ["agent-presentator"]
