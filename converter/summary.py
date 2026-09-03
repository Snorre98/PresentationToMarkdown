"""Per-presentation RAG index and standardized summary header.

After conversion and polish, this opt-in pass (``SUMMARY_ENABLED``) indexes the
presentation's slides into ``ptm.sqlite`` — one chunk per slide, embedded with a
local embeddings endpoint and stored in a sqlite-vec table partitioned by
document — then retrieves the most salient chunks and has a dedicated chat model
write a standardized English summary, prepended to the Markdown file.

The summary format is deterministic: a ``# Summary`` block with fixed
``## Abstract``, ``## Key Topics``, ``## Key Takeaways``, ``## Key Terms`` and
``## Metadata`` sections whose bullet counts scale with the number of concepts
detected in the content (capped at 16/12/8). Metadata is
always computed locally, never by the model. If the model's output does not
parse/validate, it is retried once and then falls back to a deterministic
extractive header (slide titles). A failure at any step degrades gracefully and
never fails the conversion.

Configuration (environment variables):

- ``SUMMARY_ENABLED`` — master switch. Default off.
- ``SUMMARY_BASE_URL`` — summary chat model server, defaults to the dedicated
  ``summary`` server (a small text model, ADR-0021) rather than the writer VLM.
- ``SUMMARY_MODEL`` — summary chat model id, defaults to the ``summary`` server's
  model (``Llama-3.2-3B-Instruct-4bit``).
- ``SUMMARY_API_KEY`` — optional bearer token (unused for local servers).
- ``EMBED_BASE_URL`` — embeddings server, default ``http://localhost:11434/v1``.
- ``EMBED_MODEL`` — embeddings model id, default ``nomic-embed-text``.
- ``EMBED_API_KEY`` — optional bearer token (unused for local servers).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from converter import config
from converter.format import _iter_slides
from converter.logstore import _connection, _lock
from converter.vision import _chat_completion, strip_code_fences
from converter.write import WRITE_API_KEY

try:
    import sqlite_vec
except ImportError:  # pragma: no cover - sqlite-vec is a soft dependency
    sqlite_vec = None

SUMMARY_BASE_URL = os.environ.get(
    "SUMMARY_BASE_URL", config.SERVERS["summary"].base_url
)
SUMMARY_MODEL = os.environ.get(
    "SUMMARY_MODEL", config.SERVERS["summary"].model
)
SUMMARY_API_KEY = os.environ.get("SUMMARY_API_KEY", WRITE_API_KEY)

EMBED_BASE_URL = os.environ.get("EMBED_BASE_URL", "http://localhost:11434/v1")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
EMBED_API_KEY = os.environ.get("EMBED_API_KEY") or None

SUMMARY_MAX_TOKENS = 900
_EMBED_TIMEOUT = 120.0
_SUMMARY_TIMEOUT = 600.0

_EMBED_DIM_KEY = "summary_embed_dim"
_MAX_CONTEXT_CHARS = 12000
_RETRIEVE_TOP_K = 12

_KNOWN_EMBED_DIMS = {
    "embeddinggemma": 768,
    "nomic-embed-text": 768,
}

_DOCS_CHUNKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS deck_documents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL UNIQUE,
    source_hash  TEXT NOT NULL,
    stem         TEXT NOT NULL,
    slide_count  INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deck_chunks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id  INTEGER NOT NULL REFERENCES deck_documents(id) ON DELETE CASCADE,
    chunk_index  INTEGER NOT NULL,
    title        TEXT,
    content      TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    UNIQUE(document_id, chunk_index)
);
"""

_SLIDE_SUFFIX_RE = re.compile(r"\s*[—–]\s*(Slide|Page)\s+\d+\s*$")
_BARE_SLIDE_RE = re.compile(r"^(Slide|Page)\s+\d+\s*$")

_PROMPT_HEADER = (
    "Summarize this lecture presentation for a study header. Write in English.\n\n"
    "Return EXACTLY these Markdown sections, in this order, with no other text:\n\n"
)
_PROMPT_FOOTER = (
    "\nRules:\n"
    "- Ground every point in the provided slides; do not invent facts or numbers.\n"
    "- Keep each bullet short and specific.\n"
    "- Use '- ' bullets only. No headings other than the ones listed.\n"
    "- Do not merge distinct points into one bullet; list each important point separately.\n"
    "- Output only the sections, with no preamble, no code fences, no commentary."
)

_SECTION_RE = re.compile(r"^##\s*(.+?)\s*$")


def _embed(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts, L2-normalizing each vector."""
    payload = {"model": EMBED_MODEL, "input": texts}
    url = EMBED_BASE_URL.rstrip("/") + "/embeddings"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    if EMBED_API_KEY:
        req.add_header("Authorization", f"Bearer {EMBED_API_KEY}")
    with urllib.request.urlopen(req, timeout=_EMBED_TIMEOUT) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    items = sorted(body["data"], key=lambda d: d.get("index", 0))
    return [_l2_normalize(item["embedding"]) for item in items]


def _l2_normalize(vec: list[float]) -> list[float]:
    arr = np.asarray(vec, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    if norm == 0:
        return [float(v) for v in vec]
    return [float(v) for v in (arr / norm)]


def _probe_dim() -> int:
    return len(_embed(["probe"])[0])


def _embed_dim() -> int:
    """Resolve the embedding dimension, skipping the probe for known models.

    A probe call is a cold round-trip to the embeddings server; for the default
    models the dimension is stable, so we reuse a hardcoded value and only probe
    unknown ``EMBED_MODEL`` ids.
    """
    known = _KNOWN_EMBED_DIMS.get(EMBED_MODEL)
    if known is not None:
        return known
    return _probe_dim()


def _load_vec(conn) -> bool:
    if sqlite_vec is None:
        return False
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        return True
    except Exception:
        return False


def _known_dim(conn) -> int | None:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (_EMBED_DIM_KEY,)
    ).fetchone()
    return int(row[0]) if row else None


def _store_dim(conn, dim: int) -> None:
    old = _known_dim(conn)
    if old is not None and old != dim:
        conn.execute("DROP TABLE IF EXISTS deck_chunk_vec")
        conn.execute("DELETE FROM deck_chunks")
        conn.execute("DELETE FROM deck_documents")
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        (_EMBED_DIM_KEY, str(dim)),
    )
    conn.commit()


def _ensure_base_tables(conn) -> None:
    conn.executescript(_DOCS_CHUNKS_SCHEMA)
    conn.commit()


def _ensure_vec_table(conn, dim: int) -> None:
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS deck_chunk_vec USING vec0("
        "chunk_id INTEGER PRIMARY KEY, document_id INTEGER partition key, "
        f"embedding FLOAT[{dim}])"
    )
    conn.commit()


def _source_hash(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def _slide_title(slide_md: str) -> str:
    for line in slide_md.splitlines():
        s = line.strip()
        if s.startswith("# "):
            title = _SLIDE_SUFFIX_RE.sub("", s[2:].strip()).strip()
            if _BARE_SLIDE_RE.match(title):
                return ""
            return title
    return ""


def _chunk_records(slides: list[str]) -> list[dict]:
    out: list[dict] = []
    for i, slide in enumerate(slides, start=1):
        content = slide.strip()
        out.append(
            {
                "chunk_index": i,
                "title": _slide_title(slide),
                "content": content,
                "content_hash": hashlib.md5(content.encode("utf-8")).hexdigest(),
            }
        )
    return out


def _upsert_document(conn, source_path: Path, source_hash: str, slide_count: int) -> int:
    source = str(source_path)
    stem = source_path.stem
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO deck_documents(source, source_hash, stem, slide_count, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(source) DO UPDATE SET
            source_hash = excluded.source_hash,
            stem = excluded.stem,
            slide_count = excluded.slide_count,
            updated_at = excluded.updated_at
        """,
        (source, source_hash, stem, slide_count, now, now),
    )
    row = conn.execute(
        "SELECT id FROM deck_documents WHERE source = ?", (source,)
    ).fetchone()
    return row[0]


def _index(source_path: Path, slides: list[str]) -> tuple[int, list[dict]]:
    """Index slides into SQLite, re-embedding only changed chunks.

    Returns ``(document_id, [chunk, ...])`` where each chunk holds ``id``,
    ``chunk_index``, ``title`` and ``content``.
    """
    if sqlite_vec is None:
        raise RuntimeError("sqlite-vec is not installed")
    source_hash = _source_hash(source_path)
    records = _chunk_records(slides)

    with _lock:
        conn = _connection()
        if not _load_vec(conn):
            raise RuntimeError("could not load the sqlite-vec extension")
        _ensure_base_tables(conn)
        dim = _known_dim(conn)
        doc_id = _upsert_document(conn, source_path, source_hash, len(slides))
        existing = dict(
            conn.execute(
                "SELECT chunk_index, content_hash FROM deck_chunks WHERE document_id = ?",
                (doc_id,),
            ).fetchall()
        )
        conn.commit()

    to_embed = [r for r in records if existing.get(r["chunk_index"]) != r["content_hash"]]
    vectors = _embed([r["content"] for r in to_embed]) if to_embed else []

    # Derive the vector dimension from the actual embedding output rather than a
    # separate probe round-trip; the probe cache in ``meta`` covers re-runs.
    if dim is None:
        if vectors:
            dim = len(vectors[0])
        else:
            dim = _embed_dim()

    with _lock:
        conn = _connection()
        _load_vec(conn)
        if dim is not None:
            _store_dim(conn, dim)
            _ensure_vec_table(conn, dim)

    with _lock:
        conn = _connection()
        _load_vec(conn)
        for record, vec in zip(to_embed, vectors):
            conn.execute(
                "DELETE FROM deck_chunk_vec WHERE chunk_id IN "
                "(SELECT id FROM deck_chunks WHERE document_id = ? AND chunk_index = ?)",
                (doc_id, record["chunk_index"]),
            )
            conn.execute(
                "DELETE FROM deck_chunks WHERE document_id = ? AND chunk_index = ?",
                (doc_id, record["chunk_index"]),
            )
            cur = conn.execute(
                "INSERT INTO deck_chunks(document_id, chunk_index, title, content, content_hash) "
                "VALUES (?, ?, ?, ?, ?)",
                (doc_id, record["chunk_index"], record["title"], record["content"], record["content_hash"]),
            )
            chunk_id = cur.lastrowid
            conn.execute(
                "INSERT INTO deck_chunk_vec(chunk_id, document_id, embedding) VALUES (?, ?, ?)",
                (chunk_id, doc_id, sqlite_vec.serialize_float32(vec)),
            )
        conn.commit()
        rows = conn.execute(
            "SELECT id, chunk_index, title, content FROM deck_chunks "
            "WHERE document_id = ? ORDER BY chunk_index",
            (doc_id,),
        ).fetchall()
    chunks = [
        {"id": r[0], "chunk_index": r[1], "title": r[2], "content": r[3]}
        for r in rows
    ]
    return doc_id, chunks


def _retrieve(conn, doc_id: int, query_vectors: list[list[float]], k: int) -> list[dict]:
    ids: list[int] = []
    seen: set[int] = set()
    for vec in query_vectors:
        blob = sqlite_vec.serialize_float32(vec)
        rows = conn.execute(
            "SELECT chunk_id FROM deck_chunk_vec "
            "WHERE embedding MATCH ? AND document_id = ? AND k = ?",
            (blob, doc_id, k),
        ).fetchall()
        for (cid,) in rows:
            if cid not in seen:
                seen.add(cid)
                ids.append(cid)
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, chunk_index, title, content FROM deck_chunks "
        f"WHERE id IN ({placeholders}) ORDER BY chunk_index",
        ids,
    ).fetchall()
    return [
        {"id": r[0], "chunk_index": r[1], "title": r[2], "content": r[3]}
        for r in rows
    ]


_TOPIC_MAX = 16
_TAKEAWAY_MAX = 12
_TERM_MAX = 8


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def _count_concepts(slides: list[str]) -> int:
    """Count top-level list items across the deck — a proxy for distinct concepts.

    Only zero-indent ``- `` / ``* `` / ``1. `` items count; nested bullets,
    headings, tables, blockquotes and fenced code are ignored.
    """
    count = 0
    fence = False
    for slide in slides:
        for raw in slide.splitlines():
            line = raw.rstrip()
            stripped = line.lstrip()
            if not stripped:
                continue
            if stripped.startswith("```"):
                fence = not fence
                continue
            if fence:
                continue
            if line.startswith((" ", "\t")):
                continue
            if stripped.startswith(("|", ">", "#")):
                continue
            if re.match(r"^([-*]\s+|\d+\.\s+)", stripped):
                count += 1
    return count


def _count_terms(slides: list[str]) -> int:
    """Count bold lead-ins (``**Term**`` / ``**Term:**``) across the deck."""
    count = 0
    fence = False
    for slide in slides:
        for raw in slide.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("```"):
                fence = not fence
                continue
            if fence:
                continue
            if line.startswith(("|", ">", "#")):
                continue
            if re.match(r"^\*\*[^*]+(?:\:\*\*|\*\*\s*:)", line):
                count += 1
    return count


def _summary_targets(slides: list[str]) -> tuple[int, int, int]:
    concepts = _count_concepts(slides)
    terms = _count_terms(slides)
    n_topics = _clamp(concepts, 3, _TOPIC_MAX)
    n_takeaways = _clamp(concepts, 3, _TAKEAWAY_MAX)
    n_terms = _clamp(terms, 0, _TERM_MAX)
    return n_topics, n_takeaways, n_terms


def _metadata(source_path: Path, slide_count: int) -> list[str]:
    return [
        f"Source: {source_path.name}",
        f"Slides: {slide_count}",
        f"Converted: {datetime.now(timezone.utc).date().isoformat()}",
    ]


def _bounded_sample(chunks: list[dict]) -> list[dict]:
    if not chunks:
        return []
    total = sum(len(c["content"]) for c in chunks)
    if total <= _MAX_CONTEXT_CHARS:
        return chunks
    n = len(chunks)
    stride = max(1, n // 6) if n > 6 else 1
    out: list[dict] = []
    budget = 0
    for i, c in enumerate(chunks):
        keep = i == 0 or i == n - 1 or i % stride == 0
        if keep and budget + len(c["content"]) <= _MAX_CONTEXT_CHARS:
            out.append(c)
            budget += len(c["content"])
    return out


def _parse_sections(md: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in md.splitlines():
        m = _SECTION_RE.match(line)
        if m:
            current = m.group(1).strip().lower()
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)
    return sections


def _find_section(sections: dict[str, list[str]], needle: str) -> list[str]:
    for key, lines in sections.items():
        if needle in key:
            return lines
    return []


def _bullets(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if s.startswith("- "):
            out.append(s[2:].strip())
    return out


def _valid(sections: dict[str, list[str]]) -> bool:
    abstract = [l.strip() for l in _find_section(sections, "abstract") if l.strip()]
    topics = _bullets(_find_section(sections, "topic"))
    takeaways = _bullets(_find_section(sections, "takeaway"))
    return bool(abstract) and bool(topics) and bool(takeaways)


def _looks_garbled(reply: str) -> bool:
    """Whether a failed summary reply is worth retrying.

    Retrying a model that produced a *thin but clean* answer (or nothing useful)
    is wasted latency — the deterministic fallback covers that case. Only a reply
    that looks structurally broken is retried: no parseable sections at all, a
    heading section that yielded no bullets, or suspiciously low word diversity.
    """
    sections = _parse_sections(reply)
    has_heading = bool(_find_section(sections, "abstract")) or bool(
        _find_section(sections, "topic")
    ) or bool(_find_section(sections, "takeaway"))
    if reply.strip() and not has_heading:
        return True
    if has_heading and not any(_bullets(lines) for lines in sections.values()):
        return True
    words = re.findall(r"[A-Za-z0-9]+", reply.lower())
    if words and len(set(words)) / len(words) < 0.4:
        return True
    return False


def _build_header(
    abstract: str,
    topics: list[str],
    takeaways: list[str],
    terms: list[str],
    metadata: list[str],
    counts: tuple[int, int, int],
) -> str:
    n_topics, n_takeaways, n_terms = counts
    topics = topics[:n_topics]
    takeaways = takeaways[:n_takeaways]
    terms = terms[:n_terms]
    lines = ["# Summary", "", "## Abstract", abstract.strip(), ""]
    if topics:
        lines += ["## Key Topics"] + ["- " + t for t in topics] + [""]
    if takeaways:
        lines += ["## Key Takeaways"] + ["- " + t for t in takeaways] + [""]
    if terms:
        lines += ["## Key Terms"] + terms + [""]
    lines += ["## Metadata"] + ["- " + m for m in metadata]
    return "\n".join(lines)


def _prompt(n_topics: int, n_takeaways: int, n_terms: int) -> str:
    sections = ["## Abstract\n<one sentence>"]
    if n_topics:
        sections.append(
            f"## Key Topics\n- <topic>\n... (up to {n_topics} bullets; include every distinct important topic)"
        )
    if n_takeaways:
        sections.append(
            f"## Key Takeaways\n- <takeaway>\n... (up to {n_takeaways} bullets; include every distinct important point)"
        )
    if n_terms:
        sections.append(f"## Key Terms\n- **<term>** — <definition>\n... (up to {n_terms} bullets)")
    return _PROMPT_HEADER + "\n\n".join(sections) + "\n" + _PROMPT_FOOTER


def _fallback_header(
    titles: list[str], metadata: list[str], counts: tuple[int, int, int]
) -> str:
    clean = [t for t in titles if t]
    abstract = clean[0] if clean else ""
    topics = clean[: counts[0]]
    return _build_header(abstract, topics, [], [], metadata, counts)


def _generate_summary(
    chunks: list[dict],
    titles: list[str],
    metadata: list[str],
    counts: tuple[int, int, int],
    warnings: list[str],
) -> str:
    n_topics, n_takeaways, n_terms = counts
    prompt = _prompt(n_topics, n_takeaways, n_terms)
    if chunks:
        titles_text = "\n".join(
            "- " + (c["title"] or "Slide " + str(c["chunk_index"])) for c in chunks
        )
    else:
        titles_text = "\n".join("- " + t for t in titles)
    context = "\n\n---\n\n".join(
        f"### {c['title'] or 'Slide ' + str(c['chunk_index'])}\n{c['content']}" for c in chunks
    )
    user = (
        prompt
        + "\n\nSlide titles:\n"
        + (titles_text or "(none)")
        + "\n\nMost relevant slides (context):\n"
        + context
    )
    for attempt in range(2):
        try:
            reply = _chat_completion(
                [{"role": "user", "content": user}],
                base_url=SUMMARY_BASE_URL,
                model=SUMMARY_MODEL,
                api_key=SUMMARY_API_KEY,
                max_tokens=SUMMARY_MAX_TOKENS,
                timeout=_SUMMARY_TIMEOUT,
            ).strip()
        except Exception as exc:
            warnings.append(f"Summary model call failed: {exc}")
            break
        reply = strip_code_fences(reply)
        sections = _parse_sections(reply)
        if _valid(sections):
            abstract = " ".join(l.strip() for l in _find_section(sections, "abstract")).strip()
            topics = _bullets(_find_section(sections, "topic"))
            takeaways = _bullets(_find_section(sections, "takeaway"))
            terms = [l.strip() for l in _find_section(sections, "term") if l.strip().startswith("- ")]
            return _build_header(abstract, topics, takeaways, terms, metadata, counts)
        if attempt == 0 and _looks_garbled(reply):
            warnings.append("Summary output was garbled; retrying once")
        else:
            break
    return _fallback_header(titles, metadata, counts)


def prepend_summary(md_path: Path, source_path: Path, warnings: list[str]) -> None:
    """Index the presentation and prepend a standardized summary header.

    No-op unless ``SUMMARY_ENABLED``; never raises.
    """
    if not config.is_enabled("summary"):
        return
    try:
        text = md_path.read_text(encoding="utf-8")
        slides = _iter_slides(text)
        if not slides:
            return
        titles = [_slide_title(s) for s in slides]
        counts = _summary_targets(slides)
        metadata = _metadata(source_path, len(slides))

        context_chunks: list[dict] = []
        try:
            doc_id, chunks = _index(source_path, slides)
            queries = [
                "main topics and learning objectives of this presentation",
                "key takeaways, conclusions, and most important points",
                "important terms and their definitions",
            ]
            top_k = _RETRIEVE_TOP_K
            query_vectors = _embed(queries)
            with _lock:
                conn = _connection()
                _load_vec(conn)
                context_chunks = _retrieve(conn, doc_id, query_vectors, top_k)
        except Exception as exc:
            warnings.append(f"RAG retrieval unavailable ({exc}); summarizing without it")
            context_chunks = []

        if not context_chunks:
            context_chunks = _bounded_sample(
                [
                    {"chunk_index": i, "title": t, "content": s}
                    for i, (t, s) in enumerate(zip(titles, slides), start=1)
                ]
            )

        header = _generate_summary(context_chunks, titles, metadata, counts, warnings)

        body = text.lstrip("\n")
        md_path.write_text(header + "\n\n" + body.rstrip("\n") + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - summary never fails the conversion
        warnings.append(f"Summary generation failed: {exc}")
