"""Relational schema (PostgreSQL 16 + pgvector)."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBEDDING_DIM = 1024


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB}


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RESOLVED = "resolved"  # verified, draft PR opened (or patch produced locally)
    FAILED_VERIFICATION = "failed_verification"
    NEEDS_HUMAN = "needs_human"  # unsupported conflicts, budget exhausted, refusal
    NO_CONFLICT = "no_conflict"
    ERROR = "error"


class Installation(TimestampMixin, Base):
    __tablename__ = "installations"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # GitHub installation id
    account_login: Mapped[str] = mapped_column(String(100))
    account_type: Mapped[str] = mapped_column(String(20), default="User")
    suspended: Mapped[bool] = mapped_column(default=False)


class Repository(TimestampMixin, Base):
    __tablename__ = "repositories"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # GitHub repository id
    installation_id: Mapped[int | None] = mapped_column(
        ForeignKey("installations.id", ondelete="SET NULL")
    )
    full_name: Mapped[str] = mapped_column(String(200), unique=True)
    default_branch: Mapped[str] = mapped_column(String(200), default="main")
    indexed_sha: Mapped[str | None] = mapped_column(String(64))


class Job(TimestampMixin, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("repo_full_name", "pr_number", "head_sha", "base_sha", name="uq_job_key"),
        Index("ix_jobs_status_created", "status", "created_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_full_name: Mapped[str] = mapped_column(String(200), index=True)
    installation_id: Mapped[int | None] = mapped_column(BigInteger)
    pr_number: Mapped[int | None] = mapped_column(Integer)
    head_ref: Mapped[str] = mapped_column(String(255))
    base_ref: Mapped[str] = mapped_column(String(255))
    head_sha: Mapped[str] = mapped_column(String(64))
    base_sha: Mapped[str] = mapped_column(String(64))
    trigger: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(32), default=JobStatus.QUEUED)
    budget_usd: Mapped[float] = mapped_column(Float)
    spent_usd: Mapped[float] = mapped_column(Float, default=0.0)
    attempts_used: Mapped[int] = mapped_column(Integer, default=0)
    result_url: Mapped[str | None] = mapped_column(String(500))
    summary: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[str | None] = mapped_column(Text)

    attempts: Mapped[list[Attempt]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="Attempt.number"
    )
    hunks: Mapped[list[ConflictHunkRow]] = relationship(cascade="all, delete-orphan")
    llm_calls: Mapped[list[LLMCall]] = relationship(cascade="all, delete-orphan")


class Attempt(TimestampMixin, Base):
    __tablename__ = "attempts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    verdict: Mapped[str] = mapped_column(String(32))
    patch: Mapped[str] = mapped_column(Text, default="")
    feedback: Mapped[str] = mapped_column(Text, default="")
    verification: Mapped[dict[str, Any]] = mapped_column(default=dict)
    job: Mapped[Job] = relationship(back_populates="attempts")


class ConflictHunkRow(Base):
    __tablename__ = "conflict_hunks"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(String(1000))
    hunk_index: Mapped[int] = mapped_column(Integer)
    category: Mapped[str] = mapped_column(String(32))
    complexity: Mapped[float] = mapped_column(Float)
    strategy: Mapped[str | None] = mapped_column(String(80))
    rationale: Mapped[str | None] = mapped_column(Text)


class LLMCall(Base):
    __tablename__ = "llm_calls"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    agent: Mapped[str] = mapped_column(String(40))
    task: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(80), index=True)
    route_reason: Mapped[str] = mapped_column(String(300))
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float)
    latency_ms: Mapped[int] = mapped_column(Integer)
    redactions: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16))
    error: Mapped[str | None] = mapped_column(Text)


class VerificationRun(Base):
    __tablename__ = "verification_runs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    attempt: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[str] = mapped_column(String(16))  # base | head | candidate
    sha: Mapped[str] = mapped_column(String(64))
    phase: Mapped[str] = mapped_column(String(16))  # setup | build | typecheck | test | acceptance
    exit_code: Mapped[int] = mapped_column(Integer)
    duration_s: Mapped[float] = mapped_column(Float)
    passed: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    log_tail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CodeChunk(Base):
    __tablename__ = "code_chunks"
    __table_args__ = (
        UniqueConstraint("repo_full_name", "path", "content_hash", name="uq_chunk"),
        Index(
            "ix_code_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_code_chunks_tsv", "tsv", postgresql_using="gin"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    repo_full_name: Mapped[str] = mapped_column(String(200), index=True)
    path: Mapped[str] = mapped_column(String(1000), index=True)
    symbol: Mapped[str | None] = mapped_column(String(300))
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('simple', coalesce(symbol, '') || ' ' || content)", persisted=True),
    )
