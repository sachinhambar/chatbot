"""
server.py
~~~~~~~~~
FastAPI chat transport server (WebSocket + date-grouped history) with a Strands agent backend.

- WebSocket: /ws/chat   (token streaming with heartbeats & cancel)
- HTTP:      /history   (date-grouped, paginated history for right panel)
- Health:    /healthz

Notes:
- CORS is wide-open by default; tighten for production.
- Persistence: SQLite via SQLAlchemy (swap DATABASE_URL for Postgres later).
- Pydantic v1/v2: we use .dict() where needed; switch to .model_dump() if standardizing on v2.

Run:
    uvicorn server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional
import uvicorn

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Response, Body, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import secrets
from pydantic import BaseModel
from typing import Optional

from strands_agent import StrandsAgentEngine  # your agent
from db import DBStore, init_db  # persistence layer

# -----------------------------
# Logging
# -----------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(
    level=LOG_LEVEL, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("chat-server")


# Hard-coded credentials (or set via env vars)
AUTH_USER = os.getenv("AUTH_USER", "hambar")
AUTH_PASS = os.getenv("AUTH_PASS", "shreeganesh")

# In-memory token store: token -> username
TOKENS: Dict[str, str] = {}


# -----------------------------
# Schemas
# -----------------------------


class ChatMessage(BaseModel):
    id: str
    role: str  # "user" | "assistant" | "system"
    content: str
    timestamp: float  # epoch seconds


# -----------------------------
# App, Engine, Store
# -----------------------------
init_db()
store = DBStore()
engine = StrandsAgentEngine()

app = FastAPI(title="Chat Server", version="1.1.0")

# CORS (tighten allow_origins in prod)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------
# Utilities
# -----------------------------
EVENT_HEARTBEAT_SEC = 25


def now_ts() -> float:
    return time.time()


def make_assistant_msg(content: str) -> ChatMessage:
    return ChatMessage(id=str(uuid.uuid4()), role="assistant", content=content, timestamp=now_ts())


def make_user_msg(content: str) -> ChatMessage:
    return ChatMessage(id=str(uuid.uuid4()), role="user", content=content, timestamp=now_ts())


@app.post("/auth/login")
def auth_login(payload: Dict[str, str] = Body(...)):
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    if username == AUTH_USER and password == AUTH_PASS:
        token = "tok_" + secrets.token_urlsafe(32)
        TOKENS[token] = username
        return {"token": token, "user": {"username": username}}
    raise HTTPException(status_code=401, detail="Invalid credentials")


# Optional: dependency to protect endpoints later
bearer = HTTPBearer(auto_error=False)


def require_auth(creds: HTTPAuthorizationCredentials = Depends(bearer)):
    if not creds or not creds.scheme.lower() == "bearer" or creds.credentials not in TOKENS:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return TOKENS[creds.credentials]  # return username

# -----------------------------
# WebSocket: /ws/chat
# -----------------------------


@app.websocket("/ws/chat")
async def ws_chat(ws: WebSocket) -> None:
    """
    Client -> server:
      { "type": "message", "text": "...", "session_id": "..." }
      { "type": "ping" }
      { "type": "cancel" }
    Server -> client:
      { "type": "ready" }
      { "type": "start", "session_id": "...", "message_id": "...", "ts": ... }
      { "type": "chunk", "delta": "...", "ts": ... }
      { "type": "done", "message": {...}, "usage": {...}, "ts": ... }
      { "type": "heartbeat", "ts": ... }
      { "type": "error", "error": "..." }
    """
    await ws.accept()
    session_id: Optional[str] = None
    last_ping = time.time()
    cancel = asyncio.Event()

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(1)
            if time.time() - last_ping > EVENT_HEARTBEAT_SEC:
                try:
                    await ws.send_json({"type": "heartbeat", "ts": now_ts()})
                except Exception:
                    break

    hb_task = asyncio.create_task(heartbeat())

    try:
        await ws.send_json({"type": "ready", "ts": now_ts()})
        while True:
            try:
                raw = await ws.receive_text()
            except WebSocketDisconnect:
                log.info("WebSocket disconnected")
                break

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "error": "invalid_json"})
                continue

            msg_type = data.get("type")
            if msg_type == "ping":
                last_ping = time.time()
                await ws.send_json({"type": "pong", "ts": now_ts()})
                continue

            if msg_type == "cancel":
                cancel.set()
                await ws.send_json({"type": "cancelled"})
                cancel = asyncio.Event()
                continue

            if msg_type != "message":
                await ws.send_json({"type": "error", "error": "unknown_type"})
                continue

            # handle chat message
            user_text = data.get("text", "")
            session_id = store.get_or_create(data.get("session_id"))
            user_msg = make_user_msg(user_text)
            store.append(session_id, user_msg)

            await ws.send_json({
                "type": "start",
                "session_id": session_id,
                "message_id": user_msg.id,
                "ts": now_ts(),
            })

            # chronological history for the engine
            hist = store.history(session_id)
            chunks: List[str] = []
            async for piece in engine.generate_stream(
                [m.model_dump() if hasattr(m, "model_dump") else m.dict()
                 for m in hist],
                cancel,
            ):
                chunks.append(piece)
                try:
                    await ws.send_json({"type": "chunk", "delta": piece, "ts": now_ts()})
                except WebSocketDisconnect:
                    log.info("Client disconnected mid-stream")
                    cancel.set()
                    break

            if not cancel.is_set():
                final_text = "".join(chunks)
                assistant_msg = make_assistant_msg(final_text)
                store.append(session_id, assistant_msg)
                await ws.send_json({
                    "type": "done",
                    "message": assistant_msg.dict(),
                    "usage": {"prompt_tokens": 0, "completion_tokens": len(chunks)},
                    "ts": now_ts(),
                })
            else:
                cancel = asyncio.Event()
    finally:
        hb_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await hb_task

# -----------------------------
# History API (date-grouped)
# -----------------------------


@app.get("/history")
def get_history(
    session_id: str = Query(..., description="Conversation/session ID"),
    before_id: Optional[int] = Query(
        None, description="Pagination cursor (older than this message id)"),
    limit: int = Query(
        100, le=500, description="Max messages to return in this page"),
):
    """
    Returns:
    {
      "session_id": "...",
      "groups": { "YYYY-MM-DD": [ {id, role, content, timestamp, created_at}, ... ] },
      "next_cursor": <oldest_id_in_page or null>
    }
    Pass next_cursor back as ?before_id=... to fetch older pages (newest-first paging).
    Grouping uses UTC date; format locally on the client for display.
    """
    data = store.history_grouped_by_day(
        session_id, before_id=before_id, page_size=limit)
    return {"session_id": session_id, **data}

# -----------------------------
# Health
# -----------------------------


@app.get("/healthz")
def health() -> Response:
    return PlainTextResponse("ok\n", status_code=200)


@app.post("/conversations")
def create_conversation(title: Optional[str] = Body(None, embed=True)):
    """
    Create a new, empty conversation and return its ID.
    Body: { "title": "Optional title" }
    """
    meta = store.create_conversation(title=title)
    return meta


@app.get("/conversations")
def list_conversations(limit: int = Query(50, le=200)):
    """
    List recent conversations with last activity and snippet.
    """
    return {"items": store.list_conversations(limit=limit)}


# -----------------------------
# Entrypoint
# -----------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("server:app", host="0.0.0.0", port=port,
                reload=os.getenv("RELOAD", "false") == "true")
