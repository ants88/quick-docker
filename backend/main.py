import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

logger = logging.getLogger("quickdocker")

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from docker.errors import DockerException, NotFound

from .docker_manager import DockerManager

dm: DockerManager


@asynccontextmanager
async def lifespan(app: FastAPI):
    global dm
    dm = DockerManager()
    yield
    dm.close()


app = FastAPI(lifespan=lifespan)


# --- Exception handler ---

@app.exception_handler(DockerException)
async def docker_exception_handler(request, exc):
    raise HTTPException(status_code=503, detail=f"Docker error: {exc}")


# --- REST endpoints ---

@app.get("/api/health")
async def health():
    result = await asyncio.to_thread(dm.health_check)
    if not result["ok"]:
        raise HTTPException(status_code=503, detail=result["error"])
    return result


@app.get("/api/projects")
async def list_projects():
    return await asyncio.to_thread(dm.list_projects)


@app.get("/api/containers")
async def list_containers():
    return await asyncio.to_thread(dm.list_containers)


@app.post("/api/compose/{project}/{action}")
async def compose_action(project: str, action: str):
    if action not in ("up", "down", "restart"):
        raise HTTPException(status_code=400, detail=f"Invalid action: {action}")
    result = await asyncio.to_thread(dm.compose_action, project, action)
    if not result["ok"]:
        raise HTTPException(status_code=500, detail=result["error"])
    return result


@app.post("/api/container/{container_id}/{action}")
async def container_action(container_id: str, action: str):
    if action not in ("start", "stop", "restart"):
        raise HTTPException(status_code=400, detail=f"Invalid action: {action}")
    result = await asyncio.to_thread(dm.container_action, container_id, action)
    if not result["ok"]:
        raise HTTPException(status_code=500, detail=result["error"])
    return result


@app.delete("/api/container/{container_id}")
async def container_remove(container_id: str):
    result = await asyncio.to_thread(dm.container_action, container_id, "remove")
    if not result["ok"]:
        raise HTTPException(status_code=500, detail=result["error"])
    return result


@app.get("/api/container/{container_id}/logs")
async def container_logs(container_id: str, tail: int = 200):
    try:
        log_gen = await asyncio.to_thread(dm.container_logs, container_id, tail)
    except NotFound:
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


# --- WebSocket: live events ---

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


# --- WebSocket: exec/shell ---

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

    # Get the raw socket - keep it blocking for the reader thread
    raw = sock._sock
    raw.settimeout(1.0)  # 1s timeout so reader thread can check for shutdown

    closed = asyncio.Event()

    async def read_from_docker():
        """Read from Docker socket in a thread, send to WebSocket."""
        import socket as _socket
        loop = asyncio.get_event_loop()
        while not closed.is_set():
            try:
                data = await loop.run_in_executor(None, lambda: raw.recv(4096))
                if not data:
                    break
                await websocket.send_bytes(data)
            except _socket.timeout:
                continue  # just loop and check closed flag
            except Exception as e:
                logger.error("exec read error: %s", e)
                break
        closed.set()

    async def write_to_docker():
        """Read from WebSocket, write to Docker socket."""
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


# --- Static files (frontend) ---

frontend_dir = Path(__file__).parent.parent / "frontend"
if frontend_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
