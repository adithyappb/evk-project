FROM python:3.12-slim

WORKDIR /app

# Install uv
RUN pip install --no-cache-dir uv

# Dependency layer (cached until lockfile changes)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Application source
COPY src/ src/
COPY seed/ seed/

# Runtime defaults — all overridable via env at deploy time
ENV EVK_MODE=local \
    LOCAL_DATA_DIR=/data \
    APP_HOST=0.0.0.0 \
    APP_PORT=8080 \
    AUTO_POLL=true \
    POLL_INTERVAL_MINUTES=60 \
    AUTH_EMAIL_DELIVERY_MODE=smtp

# /data is a persistent volume — the JSON store lives here
VOLUME ["/data"]
EXPOSE 8080

# PORT is injected by Railway / Render / Fly; falls back to APP_PORT
CMD ["sh", "-c", "uv run uvicorn evk.api:app --host 0.0.0.0 --port ${PORT:-${APP_PORT:-8080}}"]
