"""
ig-sidecar: FastAPI server wrapping instagrapi for headless-controller.

Routes:
  POST /accounts/{id}/login      — login with sessionid cookie
  POST /accounts/{id}/logout     — logout & stop realtime
  GET  /accounts/{id}/profile    — {username, user_id}
  POST /accounts/{id}/send       — send DM to thread_id
  POST /accounts/{id}/typing     — send typing indicator
  GET  /accounts/{id}/events     — SSE stream of incoming messages
  GET  /health                   — liveness check
"""

import asyncio
import json
import logging
import os
import queue
import threading
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from account_manager import AccountManager, IGMessage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("ig-sidecar")

RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "")

manager = AccountManager()

# Per-account SSE queues: account_id → list of asyncio.Queue
_sse_queues: dict[str, list[asyncio.Queue]] = {}
_sse_lock = threading.Lock()
_main_loop: asyncio.AbstractEventLoop | None = None


def _get_or_create_queues(account_id: str) -> list[asyncio.Queue]:
    with _sse_lock:
        if account_id not in _sse_queues:
            _sse_queues[account_id] = []
        return _sse_queues[account_id]


def _push_to_sse(account_id: str, payload: dict) -> None:
    """Called from realtime background thread — thread-safe."""
    if _main_loop is None:
        return
    with _sse_lock:
        queues = _sse_queues.get(account_id, [])
    for q in queues:
        _main_loop.call_soon_threadsafe(q.put_nowait, payload)


def _on_message(msg: IGMessage) -> None:
    _push_to_sse(
        msg.account_id,
        {
            "type": "message",
            "thread_id": msg.thread_id,
            "item_id": msg.item_id,
            "user_id": msg.user_id,
            "text": msg.text,
            "timestamp": msg.timestamp,
            "is_group": msg.is_group,
        },
    )


def _on_error(account_id: str, exc: Exception) -> None:
    _push_to_sse(account_id, {"type": "error", "error": str(exc)})


manager.on_message(_on_message)
manager.on_error(_on_error)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _main_loop
    _main_loop = asyncio.get_event_loop()
    yield


app = FastAPI(title="ig-sidecar", lifespan=lifespan)


# --- Request models ---

class LoginBody(BaseModel):
    sessionid: str
    rapidapi_key: str = ""


class SendBody(BaseModel):
    thread_id: str
    text: str


class ResolveUserBody(BaseModel):
    username: str


class TypingBody(BaseModel):
    thread_id: str


# --- Routes ---

@app.get("/health")
def health():
    return {"ok": True}


@app.post("/accounts/{account_id}/login")
def login(account_id: str, body: LoginBody):
    try:
        result = manager.login(
            account_id,
            body.sessionid,
            rapidapi_key=body.rapidapi_key or RAPIDAPI_KEY,
        )
        return result
    except Exception as exc:
        log.exception("login failed for %s", account_id)
        raise HTTPException(status_code=401, detail=str(exc))


@app.post("/accounts/{account_id}/logout")
def logout(account_id: str):
    manager.logout(account_id)
    return {"ok": True}


@app.get("/accounts/{account_id}/profile")
def profile(account_id: str):
    try:
        return manager.get_profile(account_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"{account_id} not logged in")


@app.post("/accounts/{account_id}/send")
def send(account_id: str, body: SendBody):
    try:
        result = manager.send_message(account_id, body.thread_id, body.text)
        return result
    except KeyError:
        raise HTTPException(status_code=404, detail=f"{account_id} not logged in")
    except Exception as exc:
        log.exception("send failed for %s", account_id)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/accounts/{account_id}/resolve-user")
def resolve_user(account_id: str, body: ResolveUserBody):
    """Resolve a username or user_id string to a numeric user_id."""
    try:
        client = manager.get_client(account_id)
        # If it's already a numeric user_id, return it directly
        if body.username.isdigit():
            return {"user_id": body.username}
        uid = client.user_id_from_username(body.username.lstrip("@"))
        return {"user_id": str(uid)}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"{account_id} not logged in")
    except Exception as exc:
        log.exception("resolve-user failed for %s", account_id)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/accounts/{account_id}/typing")
def typing(account_id: str, body: TypingBody):
    try:
        manager.send_typing(account_id, body.thread_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"{account_id} not logged in")
    except Exception:
        pass
    return {"ok": True}


@app.get("/accounts/{account_id}/events")
async def events(account_id: str, request: Request):
    """SSE stream for incoming DM events for this account."""
    q: asyncio.Queue = asyncio.Queue()
    queues = _get_or_create_queues(account_id)
    queues.append(q)

    async def stream() -> AsyncGenerator[str, None]:
        try:
            yield ": ping\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=30.0)
                    yield f"data: {json.dumps(payload)}\n\n"
                except asyncio.TimeoutError:
                    # Keep-alive comment
                    yield ": keepalive\n\n"
        finally:
            with _sse_lock:
                try:
                    queues.remove(q)
                except ValueError:
                    pass

    return StreamingResponse(stream(), media_type="text/event-stream")
