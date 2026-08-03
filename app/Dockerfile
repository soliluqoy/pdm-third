# syntax=docker/dockerfile:1
# =============================================================================
# PREDICT — all-in-one app image
#   stage 1: build the React SPA (web/)
#   stage 2: FastAPI backend + Teltonika TCP listener + built SPA
# Exposes: 8000 (HTTP: API + dashboard)  5123 (Teltonika AVL TCP)
# =============================================================================

# ── Stage 1: frontend build ──────────────────────────────────────────────────
FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json ./
RUN npm install --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY app/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./
COPY --from=web /web/dist ./static

EXPOSE 8000 5123
CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000"]
