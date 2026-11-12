# ai-panel's image: the app in a virtual environment, ipmitool and ssh from Debian.
# Runs as a non-root user, works with a read-only root file system (it writes only to /tmp and
# the state directory), and needs no shell: every command below the build is in exec form.

FROM ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff AS uv

FROM python:3.15-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS build
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

FROM python:3.15-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
RUN apt-get update \
 && apt-get install -y --no-install-recommends ipmitool openssh-client tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 10001 --user-group --no-create-home --home-dir /nonexistent \
      --shell /usr/sbin/nologin aipanel \
 && install -d -o 10001 -g 10001 -m 0750 /var/lib/aipanel
COPY --from=build /app/.venv /app/.venv

# HOME=/tmp: ssh wants a home; it writes nothing there (every option is on its command line).
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp \
    AIPANEL_STATE_DIR=/var/lib/aipanel AIPANEL_PORT=8080 AIPANEL_METRICS_PORT=9090
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
