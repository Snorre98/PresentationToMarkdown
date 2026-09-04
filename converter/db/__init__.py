"""SQLAlchemy persistence layer for the PresentationToMarkdown converter (ADR-0026).

This subpackage is the single ORM/engine/repository layer over ``ptm.sqlite``,
replacing the per-module raw ``sqlite3`` code that formerly lived in
``converter.logstore``, ``converter.settings`` and ``converter.summary``.

It is UI-free and import-safe: the public modules only depend on SQLAlchemy and
the standard library.
"""
from __future__ import annotations

from converter.db.engine import get_engine, get_session, reset
from converter.db.models import (
    Base,
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

__all__ = [
    "Base",
    "ConversionRun",
    "DeckChunk",
    "DeckDocument",
    "Meta",
    "RecentFile",
    "RunConfig",
    "RunPhase",
    "TranscriptSegment",
    "VisionEvent",
    "get_engine",
    "get_session",
    "reset",
]
