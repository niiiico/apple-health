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
# --no-editable: the default installs a link back to /app/src, which the
# runtime stage does not carry, so the console scripts would find no package.
RUN uv sync --frozen --no-dev --extra pg --no-editable


FROM python:3.12-slim

# Runs unprivileged. It writes nothing: every fact lives in Postgres.
RUN useradd --create-home --uid 10001 health

COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER health
WORKDIR /home/health

# No ENTRYPOINT: this package publishes several console scripts rather than one
# binary with subcommands, so the workload names the one it wants.
CMD ["ah-web", "--host", "127.0.0.1", "--port", "8765"]
