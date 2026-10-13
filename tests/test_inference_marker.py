"""Tests for the ``-inference-used`` marker on model-produced Markdown (ADR-0050)."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import converter.inference as inference
import converter.summary as summary
from converter import config, convert_file, convert_files
from converter.db import engine as db_engine
from converter.organize import (
    INFERENCE_MARKER,
    avoid_marked_artifact,
    claim_inference_path,
    is_marked_stem,
    marked_stem,
)

TESTS_DIR = Path(__file__).parent
PPTX = TESTS_DIR / "test_deck.pptx"
PDF = TESTS_DIR / "test_doc.pdf"


@pytest.fixture(autouse=True)
def _reset_config(monkeypatch):
    monkeypatch.setenv("NEED_GATE", "off")
    monkeypatch.delenv("INFERENCE_OVERWRITE", raising=False)
    config.reset()
    yield
    config.reset()


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("VISION_LOG_DB", str(tmp_path / "test.sqlite"))
    db_engine.reset()
    yield tmp_path / "test.sqlite"
    db_engine.reset()


@pytest.fixture
def fake_embed(monkeypatch):
    """Deterministic embedding so the summary pass runs without a server."""

    def _fake(texts):
        return [[1.0, 0.0, 0.0, 0.0]] * len(texts)

    monkeypatch.setattr(summary, "_embed", _fake)
    return _fake


def _vision_stub(text: str = "IMAGE TRANSCRIPTION"):
    return lambda *args, **kwargs: text


# --- ledger -------------------------------------------------------------------


def test_ledger_is_scoped_to_one_conversion():
    inference.note("vision")  # outside tracking(): a silent no-op
    assert not inference.used()

    with inference.tracking():
        assert not inference.used()
        inference.note("summary")
        inference.note("vision")
        inference.note("vision")  # idempotent
        assert inference.used()
        assert inference.passes() == ["vision", "summary"]

    assert not inference.used()  # reset when the conversion ends


# --- naming helpers -----------------------------------------------------------


def test_marked_stem_never_doubles_the_marker():
    assert marked_stem("deck") == f"deck{INFERENCE_MARKER}"
    assert marked_stem("deck-inference-used") == "deck-inference-used"
    assert marked_stem("deck-inference-used-2") == "deck-inference-used-2"


def test_is_marked_stem_cases():
    assert is_marked_stem("deck-inference-used")
    assert is_marked_stem("deck-inference-used-12")
    assert not is_marked_stem("deck")
    assert not is_marked_stem("inference-used-notes")


def test_claim_inference_path_bumps_with_a_warning(tmp_path):
    artifact = tmp_path / "deck-inference-used.md"
    artifact.write_text("keep me", encoding="utf-8")
    warnings: list[str] = []

    claimed = claim_inference_path(tmp_path, "deck", warnings)

    assert claimed.name == "deck-inference-used-2.md"
    assert warnings and "already exists" in warnings[0]
    assert artifact.read_text(encoding="utf-8") == "keep me"


def test_claim_inference_path_overwrite_opts_in(tmp_path):
    artifact = tmp_path / "deck-inference-used.md"
    artifact.write_text("old", encoding="utf-8")
    warnings: list[str] = []

    claimed = claim_inference_path(tmp_path, "deck", warnings, overwrite=True)

    assert claimed == artifact
    assert warnings == []


def test_avoid_marked_artifact_protects_a_plain_source(tmp_path):
    (tmp_path / "deck-inference-used.md").write_text("artifact", encoding="utf-8")
    warnings: list[str] = []

    assert avoid_marked_artifact(tmp_path, "deck-inference-used", warnings) == (
        "deck-inference-used-2"
    )
    assert warnings
    # Unmarked names — the common case — are returned unchanged.
    assert avoid_marked_artifact(tmp_path, "deck", []) == "deck"


# --- end-to-end: the marker lands where the content comes from ----------------


def test_deterministic_output_keeps_its_plain_name(tmp_path):
    src = tmp_path / "Deck One.pptx"
    shutil.copy(PPTX, src)

    result = convert_file(src)

    assert result.error is None
    assert result.inference_passes == []
    assert result.md_path == tmp_path / "deck-one.md"
    assert not (tmp_path / "deck-one-inference-used.md").exists()


def test_vision_content_marks_the_output(tmp_path, monkeypatch):
    config.set_enabled("vision", True)
    monkeypatch.setattr("converter.pptx.maybe_transcribe_image", _vision_stub())
    src = tmp_path / "Deck One.pptx"
    shutil.copy(PPTX, src)

    result = convert_file(src)

    assert result.error is None
    assert result.inference_passes == ["vision"]
    assert result.md_path == tmp_path / "deck-one-inference-used.md"
    assert result.md_path.exists()
    assert not (tmp_path / "deck-one.md").exists()
    assert "IMAGE TRANSCRIPTION" in result.md_path.read_text(encoding="utf-8")


def test_enabled_flag_without_model_output_is_not_marked(tmp_path, monkeypatch):
    """A vision pass that produces nothing (decorative/unreadable) leaves no trace."""
    config.set_enabled("vision", True)
    monkeypatch.setattr("converter.pptx.maybe_transcribe_image", lambda *a, **kw: None)
    src = tmp_path / "deck.pptx"
    shutil.copy(PPTX, src)

    result = convert_file(src)

    assert result.error is None
    assert result.inference_passes == []
    assert result.md_path == tmp_path / "deck.md"


def test_explicit_output_folder_is_marked_too(tmp_path, monkeypatch):
    config.set_enabled("vision", True)
    monkeypatch.setattr("converter.pptx.maybe_transcribe_image", _vision_stub("FIG"))
    src = tmp_path / "Deck One.pptx"
    shutil.copy(PPTX, src)
    out = tmp_path / "out"

    result = convert_file(src, out)

    assert result.error is None
    assert result.md_path == out / "Deck One-inference-used.md"
    assert result.inference_passes == ["vision"]
    assert src.exists()  # an explicit output folder leaves the source alone


def test_pdf_page_transcription_marks_the_output(tmp_path, monkeypatch):
    import converter.pdf as pdf

    config.set_enabled("vision", True)
    # Force the page onto the vision path without building a scan fixture.
    monkeypatch.setattr(pdf, "text_layer_quality", lambda texts: "sparse")
    monkeypatch.setattr(
        pdf, "transcribe_columns", lambda *a, **kw: "CLEAN PAGE TRANSCRIPTION"
    )
    src = tmp_path / "deck.pdf"
    shutil.copy(PDF, src)

    result = convert_file(src)

    assert result.error is None
    assert result.inference_passes == ["vision"]
    assert result.md_path.name == "deck-inference-used.md"


def test_diagram_interpretation_marks_the_output(tmp_path, monkeypatch):
    import converter.pdf as pdf

    config.set_enabled("vision", True)
    config.set_enabled("interpret", True)
    monkeypatch.setattr(pdf, "text_layer_quality", lambda texts: "sparse")
    monkeypatch.setattr(pdf, "detect_bands", lambda *a, **kw: [(0.0, 612.0)])
    monkeypatch.setattr(pdf, "interpret_diagram", lambda *a, **kw: "- A → B")
    src = tmp_path / "deck.pdf"
    shutil.copy(PDF, src)

    result = convert_file(src)

    assert result.error is None
    assert result.inference_passes == ["interpret"]
    assert result.md_path.name == "deck-inference-used.md"


def test_structure_amendments_mark_the_output(tmp_path, monkeypatch):
    import converter.pdf as pdf

    config.set_enabled("structure", True)
    monkeypatch.setenv("PDF_MODE", "paper")
    monkeypatch.setattr(
        pdf, "structure_paper", lambda pages, warnings=None, source="": ["# Amended"]
    )
    src = tmp_path / "paper.pdf"
    shutil.copy(PDF, src)

    result = convert_file(src)

    assert result.error is None
    assert result.inference_passes == ["structure"]
    assert result.md_path.name == "paper-inference-used.md"


def test_batch_marks_only_the_file_that_got_model_output(tmp_path, monkeypatch):
    config.set_enabled("vision", True)
    calls: list[int] = []

    def _first_image_only(blob, ext, warnings=None, **kwargs):
        """Only the first conversion's image produces model output."""
        calls.append(1)
        return "FIRST IMAGE TRANSCRIPTION" if len(calls) == 1 else None

    monkeypatch.setattr("converter.pptx.maybe_transcribe_image", _first_image_only)
    a = tmp_path / "a.pptx"
    b = tmp_path / "b.pptx"
    shutil.copy(PPTX, a)
    shutil.copy(PPTX, b)

    results = convert_files([a, b])

    assert results[0].md_path.name == "a-inference-used.md"
    assert results[0].inference_passes == ["vision"]
    assert results[1].md_path.name == "b.md"
    assert results[1].inference_passes == []


# --- re-runs: a marked file is never overwritten -------------------------------


def test_inference_rerun_bumps_instead_of_overwriting(tmp_path, monkeypatch):
    config.set_enabled("vision", True)
    monkeypatch.setattr("converter.pptx.maybe_transcribe_image", _vision_stub())
    src = tmp_path / "deck.pptx"
    shutil.copy(PPTX, src)

    first = convert_file(src)
    assert first.md_path.name == "deck-inference-used.md"

    second = convert_file(first.moved_to)  # re-run of the archived source

    assert second.error is None
    assert second.md_path.name == "deck-inference-used-2.md"
    assert first.md_path.exists()
    assert any("already exists" in w for w in second.warnings)


def test_overwrite_inference_replaces_the_artifact(tmp_path, monkeypatch):
    config.set_enabled("vision", True)
    monkeypatch.setattr("converter.pptx.maybe_transcribe_image", _vision_stub())
    src = tmp_path / "deck.pptx"
    shutil.copy(PPTX, src)
    first = convert_file(src)
    first.md_path.write_text("SENTINEL\n", encoding="utf-8")

    monkeypatch.setenv("INFERENCE_OVERWRITE", "on")
    second = convert_file(first.moved_to)

    assert second.md_path == first.md_path
    assert "SENTINEL" not in second.md_path.read_text(encoding="utf-8")
    assert not (tmp_path / "deck-inference-used-2.md").exists()


def test_a_source_named_like_a_marked_file_cannot_clobber_it(tmp_path):
    artifact = tmp_path / "deck-inference-used.md"
    artifact.write_text("EXPENSIVE ARTIFACT\n", encoding="utf-8")
    src = tmp_path / "deck-inference-used.pptx"
    shutil.copy(PPTX, src)

    result = convert_file(src)

    assert result.error is None
    assert result.inference_passes == []
    assert result.md_path.name == "deck-inference-used-2.md"
    assert artifact.read_text(encoding="utf-8") == "EXPENSIVE ARTIFACT\n"
    assert any("already exists" in w for w in result.warnings)


def test_duplicate_if_exists_composes_with_the_marker(tmp_path, monkeypatch):
    config.set_enabled("vision", True)
    monkeypatch.setattr("converter.pptx.maybe_transcribe_image", _vision_stub())
    src = tmp_path / "deck.pptx"
    shutil.copy(PPTX, src)
    (tmp_path / "deck.md").write_text("prior\n", encoding="utf-8")

    result = convert_file(src, duplicate_if_exists=True)

    assert result.md_path.name == "deck (2)-inference-used.md"
    assert (tmp_path / "deck.md").read_text(encoding="utf-8") == "prior\n"


def test_an_archived_deterministic_rerun_leaves_the_marked_file_alone(
    tmp_path, monkeypatch
):
    """The promise of ADR-0050: cheap re-runs cannot destroy expensive output."""
    config.set_enabled("vision", True)
    monkeypatch.setattr("converter.pptx.maybe_transcribe_image", _vision_stub())
    src = tmp_path / "deck.pptx"
    shutil.copy(PPTX, src)
    marked = convert_file(src)
    assert marked.md_path.name == "deck-inference-used.md"

    config.set_enabled("vision", False)
    monkeypatch.setattr("converter.pptx.maybe_transcribe_image", lambda *a, **kw: None)
    plain = convert_file(marked.moved_to)

    assert plain.md_path == tmp_path / "deck.md"
    assert marked.md_path.exists()
    assert "IMAGE TRANSCRIPTION" in marked.md_path.read_text(encoding="utf-8")
    assert plain.inference_passes == []


# --- the format and summary note sites ----------------------------------------


def test_accepted_restructure_marks_the_conversion(monkeypatch):
    from converter.format import polish_text

    config.set_enabled("format", True)
    monkeypatch.setattr(
        "converter.format._chat_completion",
        lambda messages, **kw: "# Slide — Page 1\n\n## The course topics\n- Modelling",
    )
    md = "# Slide — Page 1\n\n- The course topics:\n  - Modelling"

    with inference.tracking():
        polish_text(md, warnings=[], allow_llm=True)
        assert inference.passes() == ["format"]


def test_rejected_restructure_does_not_mark(monkeypatch):
    from converter.format import polish_text

    config.set_enabled("format", True)
    monkeypatch.setattr(
        "converter.format._chat_completion",
        lambda messages, **kw: "# Slide — Page 1\n\nInvented replacement words appear here",
    )
    md = "# Slide — Page 1\n\n- The course topics:\n  - Modelling"

    with inference.tracking():
        polish_text(md, warnings=[], allow_llm=True)
        assert not inference.used()


def test_unedited_restructure_does_not_mark(monkeypatch):
    from converter.format import polish_text

    config.set_enabled("format", True)
    monkeypatch.setattr(
        "converter.format._chat_completion",
        lambda messages, **kw: "# Slide — Page 1\n\n- The course topics:\n  - Modelling",
    )
    md = "# Slide — Page 1\n\n- The course topics:\n  - Modelling"

    with inference.tracking():
        polish_text(md, warnings=[], allow_llm=True)
        assert not inference.used()


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


def _prepend_summary_with(md_path: Path, tmp_path: Path, reply: str, monkeypatch):
    """Run the summary pass and return the ledger's passes (empty when fallback)."""
    config.set_enabled("summary", True)
    monkeypatch.setattr(summary, "_chat_completion", lambda messages, **kw: reply)
    with inference.tracking():
        summary.prepend_summary(md_path, tmp_path / "deck.pptx", warnings=[])
        recorded = inference.passes()
    return recorded


def test_model_summary_marks_the_conversion(
    tmp_path, monkeypatch, isolated_db, fake_embed
):
    md = tmp_path / "deck.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")

    recorded = _prepend_summary_with(md, tmp_path, VALID_REPLY, monkeypatch)

    assert recorded == ["summary"]
    assert md.read_text(encoding="utf-8").startswith("# Summary\n")


def test_fallback_summary_does_not_mark(
    tmp_path, monkeypatch, isolated_db, fake_embed
):
    md = tmp_path / "deck.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")

    recorded = _prepend_summary_with(md, tmp_path, "no usable sections at all", monkeypatch)

    assert recorded == []
    assert md.read_text(encoding="utf-8").startswith("# Summary\n")  # deterministic header
