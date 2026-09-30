# kalshibot: Kalshi PAPER-trading bot + dashboard, one container.
#
# Stage 1 builds the React dashboard; stage 2 is the Python runtime that serves it
# (FastAPI serves frontend/dist at /) alongside the API and the trading engine.
# The app locates frontend/dist and research/ relative to the package, so the repo
# layout is kept under /app. Runtime state (data/), config.yaml and research/ are
# bind-mounted by docker-compose.yml; nothing stateful lives in the image.

# ---- frontend build ---------------------------------------------------------------
FROM node:22-slim AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- python runtime ---------------------------------------------------------------
FROM python:3.11-slim AS runtime
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH \
    KALSHIBOT_SERVER__HOST=0.0.0.0 \
    KALSHIBOT_SERVER__PORT=8765

WORKDIR /app

# Dependencies first (cached layer), then the project itself (installed editable, so
# package-relative paths resolve to /app).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY kalshibot/ ./kalshibot/
COPY config.example.yaml README.md ./
RUN uv sync --frozen --no-dev
COPY --from=frontend /app/frontend/dist ./frontend/dist

RUN mkdir -p /app/data /app/research

EXPOSE 8765
# Same clean-shutdown path as Ctrl-C.
STOPSIGNAL SIGINT
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/api/status', timeout=4).status == 200 else 1)"

CMD ["kalshibot", "serve"]
