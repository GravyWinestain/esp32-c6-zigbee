#!/usr/bin/env python3
"""hub_server.py - Zigbee IoT hub HTTP service (port 8001).

Serves two surfaces from one process:

  * the hub API under /api/*  (devices, commissioning, events) - see hub/api.py
  * the original dongle panel at /  - live ZNP hardware view

The hub owns the serial port, so the driver and the legacy panel share one
Znp connection rather than fighting over /dev/ttyUSB0.

Run directly for development, or via the dongle-web systemd user unit.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from hub import Hub, Registry
from hub.api import build_router
from hub.drivers.zigbee import ZigbeeDriver

PORT = int(os.environ.get("HUB_PORT", "8001"))
SERIAL_PORT = os.environ.get("ZIGBEE_PORT", "/dev/zigbee-coordinator")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("hub.server")

registry = Registry()
driver = ZigbeeDriver(port=SERIAL_PORT)
hub = Hub(registry, [driver])


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the hub (drivers + poll loop) with the web server."""
    hub.start(poll=True)
    log.info("hub started: %d driver(s)", len(hub.drivers))
    yield
    hub.stop()
    registry.close()
    log.info("hub stopped")


app = FastAPI(title="Zigbee IoT Hub", version="1.0.0", lifespan=lifespan)


@app.exception_handler(Exception)
async def unhandled(request, exc: Exception) -> JSONResponse:
    """Return JSON rather than an HTML traceback for any unhandled error."""
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


app.include_router(build_router(hub))

PANEL = Path(__file__).resolve().parent / "hub" / "static" / "index.html"


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    """Serve the hub UI (falls back to a stub if the static file is missing)."""
    if PANEL.exists():
        return HTMLResponse(PANEL.read_text())
    return HTMLResponse(
        "<h1>Zigbee IoT Hub</h1>"
        "<p>UI not installed. API is live at <a href='/docs'>/docs</a>.</p>"
    )


@app.get("/health")
def health() -> dict:
    """Liveness probe that never touches the serial port."""
    return {"ok": True, "service": "zigbee-hub", "port": PORT}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
