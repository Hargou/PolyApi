"""
WebSocket broadcaster to browser clients.
Manages connections and sends unified stream messages.
"""

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Set

from fastapi import WebSocket

log = logging.getLogger(__name__)


class Feed:
    """Manages browser WebSocket connections and broadcasts messages."""

    def __init__(self):
        self._clients: Set[WebSocket] = set()
        self._snapshot_data: Dict[str, Any] = {
            "prices": {},
            "markets": [],
        }

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._clients.add(ws)
        log.info("Browser connected (%d total)", len(self._clients))
        await self.send_snapshot(ws)

    def disconnect(self, ws: WebSocket):
        self._clients.discard(ws)
        log.info("Browser disconnected (%d remaining)", len(self._clients))

    async def send_snapshot(self, ws: WebSocket):
        """Send current state to a newly connected client."""
        msg = {
            "type": "snapshot",
            "prices": self._snapshot_data["prices"],
            "markets": self._snapshot_data["markets"],
            "ts": int(time.time() * 1000),
        }
        try:
            await ws.send_json(msg)
        except Exception:
            pass

    def update_snapshot_prices(self, prices: dict):
        self._snapshot_data["prices"] = prices

    def update_snapshot_markets(self, markets: list):
        self._snapshot_data["markets"] = markets

    async def broadcast(self, message: dict):
        """Send a message to all connected clients."""
        if not self._clients:
            return
        dead = []
        data = json.dumps(message)
        for ws in self._clients:
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)
