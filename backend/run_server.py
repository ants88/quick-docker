#!/usr/bin/env python3
"""Standalone entry point for PyInstaller-bundled backend."""
import sys
import os
import asyncio
import json
import logging
import socket as _socket
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from docker.errors import DockerException, NotFound

from docker_manager import DockerManager

logger = logging.getLogger("quickdocker")

dm: DockerManager


@asynccontextmanager
async def lifespan(app: FastAPI):
    global dm
    dm = DockerManager()
    yield
    dm.close()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(DockerException)
async def docker_exception_handler(request, exc):
    raise HTTPException(status_code=503, detail=f"Docker error: {exc}")


@app.get("/api/health")
async def health():
    logger.info("Health check requested")
    result = await asyncio.to_thread(dm.health_check)
    if not result["ok"]:
        logger.error("Health check failed: %s", result["error"])
        raise HTTPException(status_code=503, detail=result["error"])
    return result


@app.get("/api/projects")
async def list_projects():
    projects = await asyncio.to_thread(dm.list_projects)
    logger.info("Listed %d projects", len(projects))
    return projects


@app.get("/api/containers")
async def list_containers():
    containers = await asyncio.to_thread(dm.list_containers)
    logger.info("Listed %d containers", len(containers))
    return containers


@app.post("/api/compose/{project}/{action}")
async def compose_action(project: str, action: str):
    logger.info("Compose %s on project '%s'", action, project)
    if action not in ("up", "down", "restart"):
        raise HTTPException(status_code=400, detail=f"Invalid action: {action}")
    result = await asyncio.to_thread(dm.compose_action, project, action)
    if not result["ok"]:
        logger.error("Compose %s failed on '%s': %s", action, project, result["error"])
        raise HTTPException(status_code=500, detail=result["error"])
    logger.info("Compose %s on '%s' succeeded", action, project)
    return result


@app.post("/api/container/{container_id}/{action}")
async def container_action(container_id: str, action: str):
    logger.info("Container %s on %s", action, container_id)
    if action not in ("start", "stop", "restart"):
        raise HTTPException(status_code=400, detail=f"Invalid action: {action}")
    result = await asyncio.to_thread(dm.container_action, container_id, action)
    if not result["ok"]:
        logger.error("Container %s failed on %s: %s", action, container_id, result["error"])
        raise HTTPException(status_code=500, detail=result["error"])
    logger.info("Container %s on %s succeeded", action, container_id)
    return result


@app.delete("/api/container/{container_id}")
async def container_remove(container_id: str):
    logger.info("Removing container %s", container_id)
    result = await asyncio.to_thread(dm.container_action, container_id, "remove")
    if not result["ok"]:
        logger.error("Remove failed on %s: %s", container_id, result["error"])
        raise HTTPException(status_code=500, detail=result["error"])
    logger.info("Container %s removed", container_id)
    return result


@app.get("/api/container/{container_id}/logs")
async def container_logs(container_id: str, tail: int = 200):
    logger.info("Opening log stream for container %s (tail=%d)", container_id, tail)
    try:
        log_gen = await asyncio.to_thread(dm.container_logs, container_id, tail)
    except NotFound:
        logger.error("Container %s not found for logs", container_id)
        raise HTTPException(status_code=404, detail="Container not found")

    async def sse_stream():
        while True:
            try:
                chunk = await asyncio.to_thread(next, log_gen)
                text = chunk.decode("utf-8", errors="replace")
                for line in text.splitlines(keepends=True):
                    escaped = json.dumps(line)
                    yield f"data: {escaped}\n\n"
            except StopIteration:
                break

    return StreamingResponse(sse_stream(), media_type="text/event-stream")


event_clients: set[WebSocket] = set()


@app.websocket("/api/ws/events")
async def ws_events(websocket: WebSocket):
    await websocket.accept()
    event_clients.add(websocket)
    try:
        while True:
            projects = await asyncio.to_thread(dm.list_projects)
            await websocket.send_json({"type": "state", "projects": projects})
            await asyncio.sleep(2)
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        event_clients.discard(websocket)


@app.websocket("/api/ws/exec/{container_id}")
async def ws_exec(websocket: WebSocket, container_id: str):
    await websocket.accept()

    try:
        exec_id, sock = await asyncio.to_thread(dm.container_exec, container_id)
    except NotFound:
        await websocket.close(code=1008, reason="Container not found")
        return
    except DockerException as e:
        await websocket.close(code=1011, reason=str(e))
        return

    logger.info("exec started for container %s, exec_id=%s", container_id, exec_id)

    raw = sock._sock
    raw.settimeout(1.0)

    closed = asyncio.Event()

    async def read_from_docker():
        loop = asyncio.get_event_loop()
        while not closed.is_set():
            try:
                data = await loop.run_in_executor(None, lambda: raw.recv(4096))
                if not data:
                    break
                await websocket.send_bytes(data)
            except _socket.timeout:
                continue
            except Exception as e:
                logger.error("exec read error: %s", e)
                break
        closed.set()

    async def write_to_docker():
        loop = asyncio.get_event_loop()
        while not closed.is_set():
            try:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                if "text" in msg:
                    text = msg["text"]
                    try:
                        parsed = json.loads(text)
                        if parsed.get("type") == "resize":
                            await asyncio.to_thread(
                                dm.exec_resize, exec_id, parsed["cols"], parsed["rows"]
                            )
                            continue
                    except (json.JSONDecodeError, KeyError):
                        pass
                    await loop.run_in_executor(None, lambda t=text: raw.sendall(t.encode()))
                elif "bytes" in msg:
                    await loop.run_in_executor(None, lambda b=msg["bytes"]: raw.sendall(b))
            except WebSocketDisconnect:
                break
            except Exception as e:
                logger.error("exec write error: %s", e)
                break
        closed.set()

    try:
        await asyncio.gather(read_from_docker(), write_to_docker())
    except Exception as e:
        logger.error("exec gather error: %s", e)
    finally:
        closed.set()
        try:
            raw.close()
        except Exception:
            pass
        try:
            await websocket.close()
        except Exception:
            pass


# --- Resolve frontend directory ---
# When bundled by PyInstaller, sys._MEIPASS points to the temp extract dir.
# In that case frontend/ is alongside the binary in the Tauri resource dir.
def _resolve_frontend():
    if getattr(sys, "frozen", False):
        # Running as PyInstaller bundle - frontend is a sibling dir
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).parent.parent
    candidate = base / "frontend"
    if candidate.is_dir():
        return candidate
    return None


frontend_dir = _resolve_frontend()
if frontend_dir:
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")


def main():
    port = int(os.environ.get("QUICKDOCKER_PORT", "18093"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
