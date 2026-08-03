"""
PREDICT — WebSocket hub (in-process, replaces Redis pub/sub).

Every browser session subscribes to /ws; services broadcast typed events:
  telemetry      live sensor patch for one vehicle
  health         vehicle health color changed
  alert          new/updated alert
  alert_resolved an alert auto-resolved (condition cleared)
  work_order     new/updated work order
  trip           trip opened/closed
  settings       a setting changed (e.g. ask-me-first)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger("predict.ws")


class WsHub:
    def __init__(self) -> None:
        self._conns: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._conns.add(ws)
        logger.debug("WS client connected (%d total)", len(self._conns))

    def disconnect(self, ws: WebSocket) -> None:
        self._conns.discard(ws)

    async def broadcast(self, event_type: str, data: dict[str, Any]) -> None:
        if not self._conns:
            return
        msg = json.dumps({
            "type": event_type,
            "data": data,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        dead = []
        for ws in self._conns:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._conns.discard(ws)

    @property
    def client_count(self) -> int:
        return len(self._conns)


hub = WsHub()
