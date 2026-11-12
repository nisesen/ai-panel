# ai-panel's image: the app in a virtual environment, ipmitool and ssh from Debian.
# Runs as a non-root user, works with a read-only root file system (it writes only to /tmp and
# the state directory), and needs no shell: every command below the build is in exec form.

FROM ghcr.io/astral-sh/uv:0.13.8@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85 AS uv

FROM python:3.14-slim-trixie@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# Dependencies first, so a code change does not reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.14-slim-trixie@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f
# Time zones come from the tzdata Python package, so every install knows the same names.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ipmitool openssh-client \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 10001 --user-group --no-create-home --home-dir /nonexistent \
      --shell /usr/sbin/nologin aipanel \
 && install -d -o 10001 -g 10001 -m 0750 /var/lib/aipanel
COPY --from=build /app/.venv /app/.venv

# HOME=/tmp: ssh wants a home; it writes nothing there (every option is on its command line).
# Run as uid 10001: ssh refuses to start for a uid without a passwd entry.
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp \
    AIPANEL_STATE_DIR=/var/lib/aipanel AIPANEL_LISTEN=0.0.0.0 AIPANEL_PORT=8080 \
    AIPANEL_METRICS_PORT=9090
USER 10001:10001
VOLUME ["/var/lib/aipanel"]
# 8080: the pages and the API (behind your authenticating proxy). 9090: /metrics and /healthz.
EXPOSE 8080 9090

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD ["aipanel", "healthcheck"]

ENTRYPOINT ["aipanel"]
CMD ["serve"]

LABEL org.opencontainers.image.title="ai-panel" \
      org.opencontainers.image.description="A small web control panel for a self-hosted GPU/LLM server" \
      org.opencontainers.image.source="https://github.com/nisesen/ai-panel" \
      org.opencontainers.image.licenses="MIT"
