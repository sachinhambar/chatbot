# db.py
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from sqlalchemy import func

from pydantic import BaseModel
from sqlalchemy import (
    create_engine, Column, String, Integer, Float, Text,
    DateTime, ForeignKey, Index
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

# ---------- Config ----------
# For Postgres later, set:
#   DATABASE_URL="postgresql+psycopg2://USER:PASS@HOST:5432/DBNAME"
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./chat.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith(
        "sqlite") else {},
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()

# ---------- Models ----------


class Conversation(Base):
    __tablename__ = "conversations"
    id = Column(String, primary_key=True)  # UUID
    title = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    messages = relationship(
        "Message", back_populates="conversation", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(String, ForeignKey(
        "conversations.id"), nullable=False, index=True)
    # 'user' | 'assistant' | 'system'
    role = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    timestamp = Column(Float, nullable=False)    # original float epoch seconds
    # derived from timestamp
    created_at = Column(DateTime(timezone=True), nullable=False)

    conversation = relationship("Conversation", back_populates="messages")


Index("idx_messages_conv_created",
      Message.conversation_id, Message.created_at.desc())

# ---------- Helpers ----------


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)

# Keep in sync with server.ChatMessage


class ChatMessage(BaseModel):
    id: str
    role: str
    content: str
    timestamp: float


class DBStore:
    """Persistence-backed store mirroring the in-memory API used by server.py."""

    def get_or_create(self, session_id: Optional[str]) -> str:
        import uuid
        sid = session_id or str(uuid.uuid4())
        db = SessionLocal()
        try:
            conv = db.get(Conversation, sid)
            if not conv:
                conv = Conversation(id=sid, title=None, created_at=utcnow())
                db.add(conv)
                db.commit()
            return sid
        finally:
            db.close()

    def append(self, session_id: str, msg: ChatMessage) -> None:
        db = SessionLocal()
        try:
            conv = db.get(Conversation, session_id)
            if not conv:
                conv = Conversation(id=session_id, created_at=utcnow())
                db.add(conv)
                db.flush()
            row = Message(
                conversation_id=session_id,
                role=msg.role,
                content=msg.content,
                timestamp=msg.timestamp,
                created_at=datetime.fromtimestamp(
                    msg.timestamp, tz=timezone.utc),
            )
            db.add(row)
            db.commit()
        finally:
            db.close()

    def history(self, session_id: str, limit: int = 500) -> List[ChatMessage]:
        """Chronological (oldest -> newest) for feeding the engine."""
        db = SessionLocal()
        try:
            rows = (
                db.query(Message)
                .filter(Message.conversation_id == session_id)
                .order_by(Message.created_at.asc(), Message.id.asc())
                .limit(limit)
                .all()
            )
            return [ChatMessage(id=str(r.id), role=r.role, content=r.content, timestamp=r.timestamp) for r in rows]
        finally:
            db.close()

    def history_grouped_by_day(self, session_id: str, before_id: Optional[int], page_size: int = 100) -> Dict[str, Any]:
        """
        Newest-first paging for UI.

        Returns:
        {
          "groups": { "YYYY-MM-DD": [ {id,role,content,timestamp,created_at}, ... ] },
          "next_cursor": <oldest_id_in_page or null>
        }
        """
        db = SessionLocal()
        try:
            q = (
                db.query(Message)
                .filter(Message.conversation_id == session_id)
                .order_by(Message.created_at.desc(), Message.id.desc())
            )
            if before_id:
                q = q.filter(Message.id < before_id)
            rows = q.limit(page_size).all()

            groups: Dict[str, List[Dict[str, Any]]] = {}
            for r in rows:
                day = r.created_at.astimezone(timezone.utc).date().isoformat()
                groups.setdefault(day, []).append({
                    "id": r.id,
                    "role": r.role,
                    "content": r.content,
                    "timestamp": r.timestamp,
                    "created_at": r.created_at.isoformat(),
                })

            next_cursor = rows[-1].id if rows else None
            return {"groups": groups, "next_cursor": next_cursor}
        finally:
            db.close()

    def create_conversation(self, title: Optional[str] = None) -> Dict[str, Any]:
        """Create a new conversation row and return its metadata."""
        import uuid
        sid = str(uuid.uuid4())
        db = SessionLocal()
        try:
            conv = Conversation(id=sid, title=title, created_at=utcnow())
            db.add(conv)
            db.commit()
            return {"id": sid, "title": title, "created_at": conv.created_at.isoformat()}
        finally:
            db.close()

    def list_conversations(self, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Return recent conversations with last message timestamp & snippet (if any).
        Ordered by last activity desc (then creation desc).
        """
        db = SessionLocal()
        try:
            # subquery: last message timestamp per conversation
            sub_last = (
                db.query(
                    Message.conversation_id.label("cid"),
                    func.max(Message.created_at).label("last_at"),
                )
                .group_by(Message.conversation_id)
                .subquery()
            )

            rows = (
                db.query(
                    Conversation.id,
                    Conversation.title,
                    Conversation.created_at,
                    sub_last.c.last_at,
                )
                .outerjoin(sub_last, sub_last.c.cid == Conversation.id)
                .order_by(
                    sub_last.c.last_at.desc().nullslast(),
                    Conversation.created_at.desc(),
                )
                .limit(limit)
                .all()
            )

            # Fetch a short snippet for the latest message (N+1; fine for small limits)
            out: List[Dict[str, Any]] = []
            for cid, title, created_at, last_at in rows:
                snippet = None
                if last_at is not None:
                    last_msg = (
                        db.query(Message.content, Message.role,
                                 Message.created_at)
                        .filter(Message.conversation_id == cid)
                        .order_by(Message.created_at.desc(), Message.id.desc())
                        .limit(1)
                        .first()
                    )
                    if last_msg:
                        snippet = (last_msg.content or "")[:140]
                out.append({
                    "id": cid,
                    "title": title,
                    "created_at": created_at.isoformat(),
                    "last_message_at": last_at.isoformat() if last_at else None,
                    "last_snippet": snippet,
                })
            return out
        finally:
            db.close()
