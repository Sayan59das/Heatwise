"""REST + WebSocket endpoints."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from app import __version__
from app.collectors.hub import SensorHub
from app.models import SensorHealth, Snapshot

log = logging.getLogger(__name__)
router = APIRouter()
_STARTED = time.time()


def _hub(request: Request) -> SensorHub:
    hub: SensorHub = request.app.state.hub
    return hub


@router.get("/api/snapshot", response_model=Snapshot)
async def get_snapshot(request: Request) -> Snapshot:
    latest = _hub(request).latest
    if latest is None:
        raise HTTPException(status_code=503, detail="Collectors are warming up; retry in a second.")
    return latest.snapshot


@router.get("/api/health")
async def get_health(request: Request) -> dict[str, Any]:
    latest = _hub(request).latest
    health: SensorHealth | None = latest.snapshot.health if latest else None
    recorder = getattr(request.app.state, "recorder", None)
    db = getattr(request.app.state, "db", None)
    return {
        "status": "ok",
        "version": __version__,
        "uptime_s": round(time.time() - _STARTED, 1),
        "snapshot_seq": latest.snapshot.seq if latest else None,
        "health": health.model_dump() if health else None,
        "storage": {
            "enabled": db is not None,
            "path": str(db.path) if db is not None else None,
            "retention_days": getattr(request.app.state, "retention_days", None),
            "rows_written": recorder.rows_written if recorder is not None else 0,
            "rows_dropped": recorder.rows_dropped if recorder is not None else 0,
        },
    }


@router.websocket("/ws/live")
async def ws_live(websocket: WebSocket) -> None:
    hub: SensorHub = websocket.app.state.hub
    await websocket.accept()
    queue = hub.subscribe()
    try:
        if hub.latest is not None:  # new clients get data immediately instead of waiting a tick
            await websocket.send_text(hub.latest.json)
        while True:
            item = await queue.get()
            await websocket.send_text(item.json)
    except (WebSocketDisconnect, RuntimeError):
        pass  # client went away (RuntimeError: send after close)
    except asyncio.CancelledError:
        raise
    finally:
        hub.unsubscribe(queue)


_VIEWER = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>ThermalSense - live JSON</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{margin:0;background:#0b0f14;color:#d6e2f0;font:13px/1.45 ui-monospace,Consolas,monospace}
  header{position:sticky;top:0;padding:10px 16px;background:#111823;border-bottom:1px solid #1f2b3a;display:flex;gap:16px}
  #dot{width:10px;height:10px;border-radius:50%;background:#e5484d;align-self:center}
  #dot.on{background:#30a46c} pre{margin:0;padding:16px;white-space:pre-wrap}
</style></head><body>
<header><span id="dot"></span><strong>ThermalSense</strong><span id="st">connecting...</span>
<a style="color:#6cb6ff" href="/api/snapshot">/api/snapshot</a><a style="color:#6cb6ff" href="/docs">/docs</a></header>
<pre id="out"></pre>
<script>
  const out = document.getElementById("out"), dot = document.getElementById("dot"), st = document.getElementById("st");
  function connect() {
    const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/live");
    ws.onopen = () => { dot.className = "on"; st.textContent = "live"; };
    ws.onmessage = (e) => { out.textContent = JSON.stringify(JSON.parse(e.data), null, 2); };
    ws.onclose = () => { dot.className = ""; st.textContent = "disconnected - retrying"; setTimeout(connect, 1500); };
  }
  connect();
</script></body></html>"""


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def viewer() -> str:
    return _VIEWER
