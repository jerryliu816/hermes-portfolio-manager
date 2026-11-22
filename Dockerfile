# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Stage 1: build the frontend.
#
# Node is needed to produce static files and nothing else, so it does not
# appear in the final image. Keeping it out removes an entire runtime and its
# CVE surface from something that will run unattended on a home network.
# ---------------------------------------------------------------------------
FROM node:24-slim AS frontend
WORKDIR /build

# Dependencies are copied first so a source-only change does not reinstall them.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./
RUN npm run build


# ---------------------------------------------------------------------------
# Stage 2: the application.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS app

# Fail fast and log immediately; a buffered traceback in a container is a
# traceback you find out about much later than you should.
# APP_ROOT tells the app where its non-package files are. Without it the
# installed package resolves paths relative to site-packages, and the bundled
# frontend is silently not served.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_ROOT=/app

# curl is for the healthcheck; postgresql-client provides pg_dump and psql,
# which the backup and restore commands shell out to.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY backend/ ./backend/
RUN pip install --no-cache-dir .

COPY alembic.ini ./
COPY --from=frontend /build/dist ./frontend/dist
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Run as a non-root user. The data directory holds original brokerage
# statements, so it is owned by that user and mode 0700 rather than being
# world-readable inside the image.
RUN useradd --create-home --uid 10001 portfolio \
    && mkdir -p /app/data/sources \
    && chown -R portfolio:portfolio /app/data \
    && chmod 700 /app/data /app/data/sources
USER portfolio

EXPOSE 8787

# Reports the app AND its extraction provider, so a container that is up but
# misconfigured is distinguishable from one that is genuinely healthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8787/health || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["serve"]
