# Two images stand here, and the first is a layer of the second. `toolchain` is
# what a deck's own build runs in: Slidev, the Chromium it exports a PDF with,
# and nothing else — no Python, no server, no source of this repository (line
# 14a). `instance` is that image plus the packaged server, so the toolchain and
# its browser are installed once however many of the two are built.
#
# Node is the base because the toolchain's version is the narrow one
# (frontend/.nvmrc); uv brings the Python that .python-version names. Every
# image named here is held by digest, because a tag is a name its owner may
# move.
FROM node:24.20.0-trixie-slim@sha256:50c3b2f6988dfc307b86e5301d69611af31f4789bdf232863b07d3b02fe55ae0 AS toolchain

# The toolchain runs as its own unprivileged user, in both images: a build's
# container names no user of its own, and the server is that same user here.
RUN useradd --create-home presentator
RUN corepack enable pnpm

# The toolchain is installed as that user, because a build writes its caches
# beside the project and Playwright keeps its browser under that user's home.
WORKDIR /app
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
# A build's container is started with the deck's own command line and nothing
# else, so the project the toolchain resolves from is this image's own place of
# work.
WORKDIR /app/frontend

FROM toolchain AS instance
USER root

# git mirrors a deck source and ssh reaches a private one (ADR 0010). Their
# versions are the ones this base image's archive carries: Debian keeps no
# older build to fall back to, so a pinned version is a build that breaks the
# day the archive moves, and the digest above is what makes the layer
# repeatable.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.10.9@sha256:10902f58a1606787602f303954cea099626a4adb02acbac4c69920fe9d278f82 /uv /usr/local/bin/uv
# The client alone: the server asks the daemon of its host to run each build in
# a container of its own, and compose hands it that socket.
COPY --from=docker:29.1.3-cli@sha256:4fa0ee1f3a7e4354c4ea34558b6d4ee32859baf4973d4c8ccc8e7fe3dd730c04 /usr/local/bin/docker /usr/local/bin/docker

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

# The directories the instance keeps its state in are made here so that an empty
# volume mounted over one belongs to the user that writes it.
RUN mkdir -p /data/database /data/mirrors /data/builds \
    && chown presentator /data/database /data/mirrors /data/builds

# Where this filesystem keeps what has to survive the container, and the address
# it answers at inside it; compose gives each directory a volume and offers the
# port to the machine alone.
ENV PRESENTATOR_DATABASE=/data/database/presentator.sqlite3 \
    PRESENTATOR_MIRRORS=/data/mirrors \
    PRESENTATOR_BUILDS=/data/builds \
    PRESENTATOR_TOOLCHAIN=/app/frontend \
    PRESENTATOR_HOST=0.0.0.0
USER presentator
WORKDIR /app
EXPOSE 8000
CMD ["agent-presentator"]
