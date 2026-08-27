# apple-health, for the arm64 k3s cluster.
#
# Two stages so the image carries the virtualenv and not the toolchain that
# built it: uv, the build caches and the git metadata all stay behind.
#
# One image, several roles — the interaction layer today, the advisor next —
# because they are the same package with different entry points, and separate
# images would be separate things to keep in step.
#
# What this image deliberately cannot do: ingest. `ah-sync` reads an iCloud
# Drive folder on the Mac that HealthSync writes to, which no pod can see
# (ADR-004). The cluster is downstream of Postgres, never upstream of it.

FROM python:3.12-slim AS build

# uv resolves and installs; it is not needed at runtime.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

WORKDIR /app

# uv manages the *project* environment, which VIRTUAL_ENV does not redirect;
# UV_PROJECT_ENVIRONMENT is what puts it somewhere the runtime stage can copy.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv

# Dependencies first, so a source change does not re-resolve them.
COPY pyproject.toml uv.lock README.md ./
# --extra pg is not optional here despite the name: psycopg is an extra so the
# Mac's launchd path can stay SQLite-only, but a pod that cannot reach Postgres
# has nothing at all to serve.
RUN uv sync --frozen --no-dev --extra pg --no-install-project

COPY src/ ./src/
COPY data/races/ ./data/races/
# --no-editable: the default installs a link back to /app/src, which the
# runtime stage does not carry, so the console scripts would find no package.
RUN uv sync --frozen --no-dev --extra pg --no-editable


FROM python:3.12-slim

# curl for the Claude Code installer; ripgrep because the CLI expects it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates ripgrep \
    && rm -rf /var/lib/apt/lists/*

# Runs unprivileged. It writes nothing: every fact lives in Postgres.
RUN useradd --create-home --uid 10001 health

COPY --from=build /opt/venv /opt/venv
# AH_REPO: config.repo_root() otherwise derives from the source-tree layout,
# which under a non-editable install lands in site-packages. Its docstring says
# a container should set this; without it race_detail finds no archive and the
# advisor is blind to every race.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AH_REPO=/app

# The race archive. File-backed by ADR-001 and deliberately outside the
# database, so `queries.race_detail` reads it from disk — without this the
# advisor is blind to every race it should be reasoning from. 12K of markdown.
# `repo_root()` walks up from the package, so this must sit beside it.
COPY --from=build /app/data/races /app/data/races

USER health

# The advisor drives the Claude Code CLI rather than the Messages API — the
# house arrangement, as biblio and braid do. Installed as the app's own user:
# the CLI lives in ~/.local/bin and keeps state in ~/.claude, neither of which
# root should own. It authenticates with CLAUDE_CODE_OAUTH_TOKEN; note that
# ANTHROPIC_API_KEY is deliberately never set, because its presence would make
# the CLI bill the metered API instead of the subscription.
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH="/home/health/.local/bin:${PATH}" \
    HOME=/home/health

WORKDIR /app

# No ENTRYPOINT: this package publishes several console scripts rather than one
# binary with subcommands, so the workload names the one it wants.
CMD ["ah-web", "--host", "127.0.0.1", "--port", "8765"]
