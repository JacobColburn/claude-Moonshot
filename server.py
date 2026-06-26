"""
Moonshot dashboard server.

Runs the trading engine as a background task and serves a real-time dashboard.
State and events are pushed to the browser over a WebSocket the instant they
happen — no polling, sub-second updates.

    python run.py            # start engine + dashboard
    open http://localhost:8000
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from engine import config as cfg
from engine.engine import MoonshotEngine
from engine.events import bus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-5s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("server")

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")

engine = MoonshotEngine()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(engine.start())
    logger.info("Dashboard: http://%s:%s", cfg.HOST, cfg.PORT)
    try:
        yield
    finally:
        engine.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="Moonshot Engine", lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse(os.path.join(WEB_DIR, "index.html"))


@app.get("/api/state")
async def api_state():
    return JSONResponse(engine.state())


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    queue = bus.subscribe()
    try:
        # Prime the client with current state + recent log history.
        await websocket.send_json({"type": "state", "data": engine.state()})
        for entry in bus.recent_log():
            await websocket.send_json({"type": "log", "data": entry})
        while True:
            message = await queue.get()
            await websocket.send_json(message)
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        logger.debug("ws closed: %s", e)
    finally:
        bus.unsubscribe(queue)


if os.path.isdir(WEB_DIR):
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
