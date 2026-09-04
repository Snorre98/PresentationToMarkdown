"""Repository/service layer over the ORM models (ADR-0026).

Plain typed functions (not class-based repositories) that encode every write and
read against the ptm.sqlite store. All write helpers are *best-effort*: they do
not raise, matching the converter's determinism rule (ADR-0009/0022) — the
facades in :mod:`converter.logstore` and :mod:`converter.settings` own the
``try/except`` and toggles, and these functions assume a live session.

The public functions here are intentionally close to the pre-ORM SQL shapes so
the migration is mechanical and the persisted bytes are identical.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, func, select, update

from converter.db.engine import get_session
from converter.db.models import (
    ConversionRun,
    DeckChunk,
    DeckDocument,
    Meta,
    RecentFile,
    RunConfig,
    RunPhase,
    TranscriptSegment,
    VisionEvent,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_vision_event(
    *,
    source: str,
    stage: str,
    run_id: int | None,
    page: int | None = None,
    image_ref: str | None = None,
    image_digest: str | None = None,
    model: str | None = None,
    decision: str | None = None,
    raw_answer: str | None = None,
    latency_ms: int | None = None,
    prompt_tokens: int | None = None,
    generated_tokens: int | None = None,
    markdown: str | None = None,
    omitted_words: list[str] | None = None,
    error: str | None = None,
    base_url: str | None = None,
) -> None:
    session = get_session()
    try:
        session.add(
            VisionEvent(
                ts=_now(),
                source=source,
                page=page,
                image_ref=image_ref,
                image_digest=image_digest,
                stage=stage,
                model=model,
                decision=decision,
                raw_answer=raw_answer,
                latency_ms=latency_ms,
                prompt_tokens=prompt_tokens,
                generated_tokens=generated_tokens,
                markdown=markdown,
                omitted_words=json.dumps(omitted_words)
                if omitted_words is not None
                else None,
                error=error,
                base_url=base_url,
                run_id=run_id,
            )
        )
        session.commit()
    finally:
        session.close()


def record_transcript_segment(
    *,
    source: str,
    start: float,
    end: float,
    text: str,
    speaker: str | None = None,
    model: str | None = None,
    error: str | None = None,
) -> None:
    session = get_session()
    try:
        session.add(
            TranscriptSegment(
                ts=_now(),
                source=source,
                start=start,
                end=end,
                speaker=speaker,
                text=text,
                model=model,
                error=error,
            )
        )
        session.commit()
    finally:
        session.close()


def start_run(source: str, name: str | None = None) -> int | None:
    session = get_session()
    try:
        run = ConversionRun(
            ts=_now(),
            source=source,
            name=name or Path(source).name or source,
            status="running",
        )
        session.add(run)
        session.commit()
        return int(run.id)
    finally:
        session.close()


def finish_run(run_id: int, status: str = "ok") -> None:
    session = get_session()
    try:
        run = session.get(ConversionRun, run_id)
        duration_ms = None
        if run is not None:
            try:
                start = datetime.fromisoformat(run.ts)
                duration_ms = int(
                    (datetime.now(timezone.utc) - start).total_seconds() * 1000
                )
            except Exception:
                duration_ms = None
        session.execute(
            update(ConversionRun)
            .where(ConversionRun.id == run_id)
            .values(
                status=status,
                ended_at=_now(),
                duration_ms=func.coalesce(duration_ms, ConversionRun.duration_ms),
            )
        )
        session.commit()
    finally:
        session.close()


def begin_phase(
    run_id: int, phase: str, ordinal: int, detail: dict | None = None
) -> int | None:
    session = get_session()
    try:
        row = RunPhase(
            run_id=run_id,
            phase=phase,
            ordinal=ordinal,
            status="running",
            started_at=_now(),
            detail=json.dumps(detail) if detail is not None else None,
        )
        session.add(row)
        session.commit()
        return int(row.id)
    finally:
        session.close()


def end_phase(
    phase_id: int,
    status: str = "done",
    duration_ms: int | None = None,
    detail: dict | None = None,
) -> None:
    session = get_session()
    try:
        session.execute(
            update(RunPhase)
            .where(RunPhase.id == phase_id)
            .values(
                status=status,
                ended_at=_now(),
                duration_ms=duration_ms,
                detail=func.coalesce(
                    json.dumps(detail) if detail is not None else None,
                    RunPhase.detail,
                ),
            )
        )
        session.commit()
    finally:
        session.close()


def snapshot_run(run_id: int, snapshot: dict) -> None:
    session = get_session()
    try:
        existing = session.get(RunConfig, run_id)
        if existing is None:
            session.add(RunConfig(run_id=run_id, snapshot=json.dumps(snapshot)))
        else:
            existing.snapshot = json.dumps(snapshot)
        session.commit()
    finally:
        session.close()


# --- Settings / meta ----------------------------------------------------------


def get_meta(key: str, default: str | None = None) -> str | None:
    session = get_session()
    try:
        row = session.get(Meta, key)
        return row.value if row is not None else default
    finally:
        session.close()


def set_meta(key: str, value: str) -> None:
    session = get_session()
    try:
        existing = session.get(Meta, key)
        if existing is None:
            session.add(Meta(key=key, value=value))
        else:
            existing.value = value
        session.commit()
    finally:
        session.close()


def record_recent_path(path: str) -> None:
    session = get_session()
    try:
        existing = session.get(RecentFile, path)
        if existing is None:
            session.add(RecentFile(path=path, last_used=_now()))
        else:
            existing.last_used = _now()
        session.commit()
    finally:
        session.close()


def recent_paths(limit: int = 10) -> list[str]:
    session = get_session()
    try:
        rows = session.execute(
            select(RecentFile.path)
            .order_by(RecentFile.last_used.desc())
            .limit(limit)
        ).scalars()
        return list(rows)
    finally:
        session.close()


# --- RAG documents / chunks ---------------------------------------------------


def upsert_document(
    source_path: Path, source_hash: str, slide_count: int
) -> int:
    source = str(source_path)
    stem = source_path.stem
    now = _now()
    session = get_session()
    try:
        doc = session.scalar(select(DeckDocument).where(DeckDocument.source == source))
        if doc is None:
            doc = DeckDocument(
                source=source,
                source_hash=source_hash,
                stem=stem,
                slide_count=slide_count,
                created_at=now,
                updated_at=now,
            )
            session.add(doc)
        else:
            doc.source_hash = source_hash
            doc.stem = stem
            doc.slide_count = slide_count
            doc.updated_at = now
        session.commit()
        return int(doc.id)
    finally:
        session.close()


def existing_chunk_hashes(document_id: int) -> dict[int, str]:
    session = get_session()
    try:
        rows = session.execute(
            select(DeckChunk.chunk_index, DeckChunk.content_hash).where(
                DeckChunk.document_id == document_id
            )
        ).all()
        return {chunk_index: content_hash for chunk_index, content_hash in rows}
    finally:
        session.close()


def upsert_chunk(
    document_id: int,
    chunk_index: int,
    title: str | None,
    content: str,
    content_hash: str,
) -> int:
    session = get_session()
    try:
        session.execute(
            delete(DeckChunk).where(
                DeckChunk.document_id == document_id,
                DeckChunk.chunk_index == chunk_index,
            )
        )
        chunk = DeckChunk(
            document_id=document_id,
            chunk_index=chunk_index,
            title=title,
            content=content,
            content_hash=content_hash,
        )
        session.add(chunk)
        session.commit()
        return int(chunk.id)
    finally:
        session.close()


def all_chunks(document_id: int) -> list[dict]:
    session = get_session()
    try:
        rows = session.execute(
            select(DeckChunk.id, DeckChunk.chunk_index, DeckChunk.title, DeckChunk.content)
            .where(DeckChunk.document_id == document_id)
            .order_by(DeckChunk.chunk_index)
        ).all()
        return [
            {"id": r[0], "chunk_index": r[1], "title": r[2], "content": r[3]}
            for r in rows
        ]
    finally:
        session.close()


def chunks_by_ids(ids: list[int]) -> list[dict]:
    session = get_session()
    try:
        rows = session.execute(
            select(DeckChunk.id, DeckChunk.chunk_index, DeckChunk.title, DeckChunk.content)
            .where(DeckChunk.id.in_(ids))
            .order_by(DeckChunk.chunk_index)
        ).all()
        return [
            {"id": r[0], "chunk_index": r[1], "title": r[2], "content": r[3]}
            for r in rows
        ]
    finally:
        session.close()


def clear_chunks() -> None:
    """Delete all deck chunks and documents (used on an embed-dim change)."""
    session = get_session()
    try:
        session.execute(delete(DeckChunk))
        session.execute(delete(DeckDocument))
        session.commit()
    finally:
        session.close()


@contextmanager
def raw_connection():
    """Yield the raw DBAPI connection underlying the writer engine.

    Required by the sqlite-vec extension (``sqlite_vec.load``) and the
    ``vec0`` ``MATCH``/``serialize_float32`` calls, which cannot run through the
    ORM (ADR-0026).
    """
    from converter.db.engine import get_engine

    engine = get_engine()
    conn = engine.raw_connection()
    try:
        yield conn
    finally:
        conn.close()
