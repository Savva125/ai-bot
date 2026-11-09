from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, ForeignKeyConstraint, Integer, JSON, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def uid() -> str:
    return str(uuid4())


def now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    external_id: Mapped[str] = mapped_column(String(120), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    adapter: Mapped[str] = mapped_column(String(40), default="demo")
    profile: Mapped[dict] = mapped_column(JSON, default=dict)
    knowledge: Mapped[str] = mapped_column(Text, default="")
    retention_days: Mapped[int] = mapped_column(Integer, default=90)


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("account_id", "external_id"), UniqueConstraint("account_id", "id"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    external_id: Mapped[str] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(String(200))
    memory: Mapped[str] = mapped_column(Text, default="")
    adult_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    marketing_allowed: Mapped[bool] = mapped_column(Boolean, default=False)


class Chat(Base):
    __tablename__ = "chats"
    __table_args__ = (
        UniqueConstraint("account_id", "external_id"), UniqueConstraint("account_id", "id"),
        ForeignKeyConstraint(["account_id", "contact_id"], ["contacts.account_id", "contacts.id"], ondelete="CASCADE"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    contact_id: Mapped[str] = mapped_column(String(36))
    external_id: Mapped[str] = mapped_column(String(120))
    revision: Mapped[str] = mapped_column(String(64), default="")


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("chat_id", "external_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    chat_id: Mapped[str] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"))
    external_id: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Draft(Base):
    __tablename__ = "drafts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    chat_id: Mapped[str] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"))
    revision: Mapped[str] = mapped_column(String(64))
    config_hash: Mapped[str] = mapped_column(String(64))
    original: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="awaiting_approval")
    reason: Mapped[str] = mapped_column(String(120), default="")
    provider: Mapped[str] = mapped_column(String(100))
    idempotency_key: Mapped[str] = mapped_column(String(36), unique=True, default=uid)
    receipt: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MessageSuppression(Base):
    """Minimal tombstones prevent deleted source messages from being reimported."""
    __tablename__ = "message_suppressions"
    chat_id: Mapped[str] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True)
    external_hash: Mapped[str] = mapped_column(String(64), primary_key=True)


class Content(Base):
    __tablename__ = "content"
    __table_args__ = (UniqueConstraint("account_id", "id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    price: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)


class Opportunity(Base):
    __tablename__ = "opportunities"
    __table_args__ = (UniqueConstraint("chat_id", "revision"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    chat_id: Mapped[str] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"))
    revision: Mapped[str] = mapped_column(String(64))
    evidence: Mapped[str] = mapped_column(Text)
    content_id: Mapped[str | None] = mapped_column(ForeignKey("content.id", ondelete="SET NULL"), nullable=True)
    suggestion: Mapped[str] = mapped_column(Text)
    caption: Mapped[str] = mapped_column(Text)
    next_step: Mapped[str] = mapped_column(Text)


class Feedback(Base):
    __tablename__ = "feedback"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    draft_id: Mapped[str] = mapped_column(ForeignKey("drafts.id", ondelete="CASCADE"))
    rating: Mapped[str] = mapped_column(String(20))
    explanation: Mapped[str] = mapped_column(Text, default="")
    proposed: Mapped[str] = mapped_column(Text)
    corrected: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ConfigVersion(Base):
    __tablename__ = "config_versions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    profile: Mapped[dict] = mapped_column(JSON)
    knowledge: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AssistantSetting(Base):
    __tablename__ = "assistant_settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class ConversationContext(Base):
    __tablename__ = "conversation_contexts"
    chat_id: Mapped[str] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True)
    turns: Mapped[list] = mapped_column(JSON, default=list)
    last_incoming_key: Mapped[str] = mapped_column(String(64), default="")
    followup_anchor: Mapped[str] = mapped_column(String(64), default="")


class ReplyExample(Base):
    __tablename__ = "reply_examples"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    draft_id: Mapped[str | None] = mapped_column(ForeignKey("drafts.id", ondelete="SET NULL"), nullable=True)
    context: Mapped[str] = mapped_column(Text)
    proposed: Mapped[str] = mapped_column(Text, default="")
    reply: Mapped[str] = mapped_column(Text)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Audit(Base):
    __tablename__ = "audit"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    action: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[str] = mapped_column(String(36), default="")
    code: Mapped[str] = mapped_column(String(120), default="ok")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
