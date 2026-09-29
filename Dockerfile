FROM python:3.13-alpine

COPY --from=ghcr.io/astral-sh/uv:0.12.20 /uv /uvx /bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY exporter.py .

ENV PATH="/app/.venv/bin:$PATH" \
    EXPORTER_PORT=9100 \
    SCRAPE_INTERVAL=60 \
    LOG_LEVEL=INFO \
    TIMEZONE=America/Los_Angeles

EXPOSE 9100

USER nobody

ENTRYPOINT ["python", "/app/exporter.py"]
