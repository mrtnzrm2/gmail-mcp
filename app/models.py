import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    full_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("now()")
    )


class GmailAccount(Base):
    __tablename__ = "gmail_accounts"
    __table_args__ = (UniqueConstraint("user_id", "gmail_email", name="uq_user_gmail_email"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    gmail_email: Mapped[str] = mapped_column(Text, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    refresh_token_enc: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("now()")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class OAuthState(Base):
    __tablename__ = "oauth_states"

    state: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    purpose: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("gmail_accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_via_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    hybrid_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    hybrid_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    hybrid_error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    intelligence_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    intelligence_hybrid_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    intelligence_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    forced_to_self: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    reply_mode: Mapped[str | None] = mapped_column(Text, nullable=True)
    override_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_to: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_to: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("now()")
    )


class ThreadSummaryCache(Base):
    __tablename__ = "thread_summaries_cache"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "account_id",
            "thread_id",
            "max_messages",
            "hybrid_enabled",
            "model",
            name="uq_thread_summary_cache_key",
        ),
        Index("ix_thread_summaries_cache_user_account_thread", "user_id", "account_id", "thread_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("gmail_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    thread_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    max_messages: Mapped[int] = mapped_column(Integer, nullable=False)
    hybrid_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    model: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    summary_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    thread_last_date: Mapped[str] = mapped_column(Text, nullable=False)
    thread_last_msg_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    thread_last_internal_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("now()"), index=True
    )
