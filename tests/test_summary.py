"""Tests for the per-presentation RAG index + summary header (converter.summary)."""
from pathlib import Path

import pytest

import converter.summary as summary
from converter import logstore


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(logstore, "VISION_LOG_DB", str(tmp_path / "test.sqlite"))
    monkeypatch.setattr(logstore, "_conn", None)
    yield tmp_path / "test.sqlite"


@pytest.fixture
def fake_embed(monkeypatch):
    """Deterministic 4-dim embedding so sqlite-vec runs without a server."""
    def _fake(texts):
        return [[1.0, 0.0, 0.0, 0.0]] * len(texts)

    monkeypatch.setattr(summary, "_embed", _fake)
    return _fake


SAMPLE_MD = (
    "# First Title — Slide 1\n\n"
    "- point one\n\n"
    '<div style="page-break-after: always; break-after: page;"></div>\n\n'
    "---\n\n"
    "# Second Title — Slide 2\n\n"
    "- point two\n\n"
    '<div style="page-break-after: always; break-after: page;"></div>\n\n'
    "---\n"
)

VALID_REPLY = (
    "## Abstract\nThis deck covers modelling and innovation.\n\n"
    "## Key Topics\n- Modelling\n- Innovation\n- Sustainability\n\n"
    "## Key Takeaways\n- Modelling matters\n- Innovation matters\n- Sustainability matters\n\n"
    "## Key Terms\n- **Model** — a representation\n"
)


def test_count_concepts():
    md = (
        "# Deck — Slide 1\n\n"
        "- one\n"
        "- two\n"
        "  - nested\n"
        "1. three\n\n"
        "| table | row |\n"
        "| --- | --- |\n"
        "> blockquote\n"
        "```\n- not a bullet\n```\n"
    )
    assert summary._count_concepts([md]) == 3


def test_count_terms():
    md = "# Deck — Slide 1\n\n**Purpose:** learn\n\n- plain bullet\n\n**Goal:** apply"
    assert summary._count_terms([md]) == 2


def test_summary_targets_scale_with_concepts():
    # 5 top-level concepts -> 5 topics/takeaways (not 3)
    slides = [f"- concept {i}" for i in range(5)]
    topics, takeaways, terms = summary._summary_targets(slides)
    assert topics == 5
    assert takeaways == 5
    assert terms == 0

    # Below the floor it still pads to 3
    topics, takeaways, terms = summary._summary_targets(["- only one"])
    assert topics == 3
    assert takeaways == 3

    # Above the ceiling it clamps at 16 / 12
    slides = [f"- concept {i}" for i in range(40)]
    topics, takeaways, terms = summary._summary_targets(slides)
    assert topics == 16
    assert takeaways == 12


def test_slide_title_strips_suffix():
    assert summary._slide_title("# Foo — Slide 1\n\nbody") == "Foo"
    assert summary._slide_title("# Foo — Page 3\n\nbody") == "Foo"
    assert summary._slide_title("# Slide 5\n\nbody") == ""


def test_parse_and_build_header():
    sections = summary._parse_sections(VALID_REPLY)
    assert summary._valid(sections)
    topics = summary._bullets(summary._find_section(sections, "topic"))
    assert topics == ["Modelling", "Innovation", "Sustainability"]
    header = summary._build_header(
        "Abstract here", topics, ["Take"], ["- **Model** — def"],
        ["Source: x.pptx"], (3, 3, 1),
    )
    assert header.startswith("# Summary\n\n## Abstract\nAbstract here")
    assert "## Key Topics" in header
    assert "## Key Terms\n- **Model** — def" in header
    assert "## Metadata\n- Source: x.pptx" in header


def test_generate_summary_success(monkeypatch):
    monkeypatch.setattr(
        summary, "_chat_completion", lambda messages, **kw: VALID_REPLY
    )
    chunks = [{"chunk_index": 1, "title": "First Title", "content": "- point"}]
    header = summary._generate_summary(
        chunks, ["First Title"], ["Source: x.pptx"], (3, 3, 1), []
    )
    assert "# Summary" in header
    assert "## Abstract\nThis deck covers modelling and innovation." in header
    assert "- Modelling" in header
    assert "## Metadata\n- Source: x.pptx" in header


def test_generate_summary_falls_back_on_error(monkeypatch):
    def _boom(messages, **kw):
        raise RuntimeError("no model")

    monkeypatch.setattr(summary, "_chat_completion", _boom)
    header = summary._generate_summary(
        [], ["First Title", "Second Title"], ["Source: x.pptx"], (3, 3, 0), []
    )
    assert "# Summary" in header
    assert "## Key Topics\n- First Title\n- Second Title" in header
    assert "## Key Takeaways" not in header
    assert "## Metadata\n- Source: x.pptx" in header


def test_generate_summary_retries_once(monkeypatch):
    calls = {"n": 0}

    def _flaky(messages, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return "garbage without sections"
        return VALID_REPLY

    monkeypatch.setattr(summary, "_chat_completion", _flaky)
    header = summary._generate_summary(
        [], ["First Title"], ["Source: x.pptx"], (3, 3, 1), []
    )
    assert calls["n"] == 2
    assert "## Abstract" in header


def test_index_and_idempotent_reembed(isolated_db, tmp_path, fake_embed):
    slides = summary._iter_slides(SAMPLE_MD)
    src = tmp_path / "fake_deck.pptx"
    src.write_bytes(b"fake")
    doc_id, chunks = summary._index(src, slides)
    assert len(chunks) == 2
    conn = logstore._connection()
    assert conn.execute("SELECT COUNT(*) FROM deck_chunks").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM deck_chunk_vec").fetchone()[0] == 2

    # Re-indexing unchanged slides must not duplicate chunks or vectors.
    doc_id2, chunks2 = summary._index(src, slides)
    assert doc_id2 == doc_id
    assert conn.execute("SELECT COUNT(*) FROM deck_chunks").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM deck_chunk_vec").fetchone()[0] == 2


def test_retrieve_scoped_to_document(isolated_db, tmp_path, fake_embed):
    src_a = tmp_path / "a.pptx"
    src_a.write_bytes(b"a")
    src_b = tmp_path / "b.pptx"
    src_b.write_bytes(b"b")
    slides = summary._iter_slides(SAMPLE_MD)
    doc_a, chunks_a = summary._index(src_a, slides)
    doc_b, chunks_b = summary._index(src_b, slides)
    assert doc_a != doc_b

    conn = logstore._connection()
    summary._load_vec(conn)
    query = [[1.0, 0.0, 0.0, 0.0]]
    found_a = summary._retrieve(conn, doc_a, query, 5)
    found_b = summary._retrieve(conn, doc_b, query, 5)
    assert {c["chunk_index"] for c in found_a} == {1, 2}
    assert {c["chunk_index"] for c in found_b} == {1, 2}
    assert {c["id"] for c in found_a}.isdisjoint({c["id"] for c in found_b})


def test_prepend_summary_end_to_end(tmp_path, isolated_db, fake_embed, monkeypatch):
    monkeypatch.setattr(summary, "SUMMARY_ENABLED", True)
    monkeypatch.setattr(
        summary, "_chat_completion", lambda messages, **kw: VALID_REPLY
    )
    md = tmp_path / "deck.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    src = tmp_path / "deck.pptx"
    src.write_bytes(b"x")

    summary.prepend_summary(md, src, warnings=[])

    text = md.read_text(encoding="utf-8")
    assert text.startswith("# Summary\n")
    assert "## Metadata\n- Source: deck.pptx" in text
    assert "# First Title — Slide 1" in text


def test_prepend_summary_disabled_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(summary, "SUMMARY_ENABLED", False)
    md = tmp_path / "deck.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    summary.prepend_summary(md, tmp_path / "deck.pptx", warnings=[])
    assert md.read_text(encoding="utf-8") == SAMPLE_MD
