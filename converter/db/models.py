"""Declarative SQLAlchemy models for the ``ptm.sqlite`` store (ADR-0026).

One model per real table, mirroring the pre-ORM schema exactly (same table
names, column names, types, primary keys, unique constraints, foreign keys and
indexes) so existing ``ptm.sqlite`` files open unchanged. The sqlite-vec
``deck_chunk_vec`` virtual table is deliberately *not* modelled here — it is a
``vec0`` virtual table that SQLAlchemy cannot introspect or map, and is handled
on the raw DBAPI connection (see ADR-0026 and :mod:`converter.summary`).
"""
from __future__ import annotations

from sqlalchemy import (
    Float,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all PTM store models."""


class Meta(Base):
    __tablename__ = "meta"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)


class VisionEvent(Base):
    __tablename__ = "vision_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    page: Mapped[int | None] = mapped_column(Integer)
    image_ref: Mapped[str | None] = mapped_column(Text)
    image_digest: Mapped[str | None] = mapped_column(Text)
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(Text)
    decision: Mapped[str | None] = mapped_column(Text)
    raw_answer: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    generated_tokens: Mapped[int | None] = mapped_column(Integer)
    markdown: Mapped[str | None] = mapped_column(Text)
    omitted_words: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    base_url: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[int | None] = mapped_column(Integer)


Index("idx_events_source", VisionEvent.source)
Index("idx_events_digest", VisionEvent.image_digest)
Index("idx_events_run", VisionEvent.run_id)


class TranscriptSegment(Base):
    __tablename__ = "transcript_segments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    start: Mapped[float] = mapped_column(Float, nullable=False)
    end: Mapped[float] = mapped_column(Float, nullable=False)
    speaker: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)


Index("idx_transcript_source", TranscriptSegment.source)


class ConversionRun(Base):
    __tablename__ = "conversion_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    ended_at: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)


Index("idx_runs_source", ConversionRun.source)


class RunPhase(Base):
    __tablename__ = "run_phases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str] = mapped_column(Text, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[str | None] = mapped_column(Text)
    ended_at: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[str | None] = mapped_column(Text)


Index("idx_phases_run", RunPhase.run_id)


class RunConfig(Base):
    __tablename__ = "run_config"

    run_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot: Mapped[str] = mapped_column(Text, nullable=False)


class DeckDocument(Base):
    __tablename__ = "deck_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    source_hash: Mapped[str] = mapped_column(Text, nullable=False)
    stem: Mapped[str] = mapped_column(Text, nullable=False)
    slide_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class DeckChunk(Base):
    __tablename__ = "deck_chunks"
    __table_args__ = (UniqueConstraint("document_id", "chunk_index"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)


class RecentFile(Base):
    __tablename__ = "recent_files"

    path: Mapped[str] = mapped_column(Text, primary_key=True)
    last_used: Mapped[str] = mapped_column(Text, nullable=False)


Index("idx_recent_last_used", RecentFile.last_used)
