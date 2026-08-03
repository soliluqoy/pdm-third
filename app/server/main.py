"""
PREDICT — application entrypoint.

One process runs everything:
  FastAPI (REST /api/v1 + WebSocket /ws + built SPA)
  Teltonika TCP listener (:5123)
  background jobs (offline watchdog, baselines/anomaly)
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from server.api import api_router
from server.config import settings
from server.db import async_session_factory
from server.ingest import handle_records, registry
from server.init_db import init_db
from server.services import baselines, watchdog
from server.teltonika.server import TeltonikaListener
from server.ws import hub

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("predict.main")

listener = TeltonikaListener(handle_records)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # DB init with a short retry loop (container start ordering).
    for attempt in range(1, 11):
        try:
            await init_db()
            break
        except Exception as e:
            logger.warning("DB init attempt %d failed: %s — retrying in 3 s", attempt, e)
            await asyncio.sleep(3)
    else:
        raise RuntimeError("Database never became ready")

    async with async_session_factory() as session:
        await registry.refresh(session)

    await listener.start()
    watchdog.start()
    baselines.start()
    logger.info("PREDICT %s up — dashboard :8000, trackers :%d",
                settings.APP_VERSION, settings.TELTONIKA_PORT)
    try:
        yield
    finally:
        await listener.stop()
        await watchdog.stop()
        await baselines.stop()


app = FastAPI(title=settings.APP_NAME, version=settings.APP_VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "ws_clients": hub.client_count,
    }


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await hub.connect(ws)
    try:
        while True:
            # Clients don't send commands; keep the socket alive.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        hub.disconnect(ws)


# ── Built SPA (production image copies web/dist → app/static) ────────────────
# Assets are hashed and can be cached forever. index.html must NOT be cached,
# or a hard refresh keeps the old bundle (and /driving would 404 without a
# client-route fallback).
_static = Path(settings.STATIC_DIR)
if _static.is_dir():
    _assets = _static / "assets"
    if _assets.is_dir():
        app.mount("/assets", StaticFiles(directory=_assets), name="assets")

    @app.get("/")
    async def spa_root():
        return FileResponse(
            _static / "index.html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        # Prefer a real static file (favicon.svg, etc.); else SPA index.
        candidate = (_static / full_path).resolve()
        try:
            candidate.relative_to(_static.resolve())
        except ValueError as exc:
            raise HTTPException(404) from exc
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(
            _static / "index.html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    logger.info("Serving dashboard from %s", _static.resolve())
else:
    logger.info("No %s directory — API-only mode (use the Vite dev server)", _static)
