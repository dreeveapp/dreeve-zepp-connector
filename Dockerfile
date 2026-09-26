FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src ./src
COPY README.md ./
RUN uv sync --frozen --no-dev

COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

# Without this, stdout is block-buffered under Docker and progress lines can
# sit unseen in the buffer for hours.
ENV PYTHONUNBUFFERED=1
ENV WATCH_DIR=/watch
ENV STATE_DIR=/state
ENV HEALTH_PORT=8080
VOLUME ["/watch", "/state"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os,urllib.request,sys; urllib.request.urlopen(f'http://localhost:{os.environ.get(\"HEALTH_PORT\",\"8080\")}/healthz', timeout=3)" || exit 1

ENTRYPOINT ["./docker-entrypoint.sh"]
