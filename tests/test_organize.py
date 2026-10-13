"""Tests for the default output layout: kebab names + source/ archive (ADR-0048)."""
from __future__ import annotations

import shutil
from pathlib import Path

from converter import collect_inputs, config, convert_file, convert_files
from converter.organize import (
    LayoutResolver,
    archive_source,
    default_output_dir,
    is_archived,
    kebab_stem,
)

TESTS_DIR = Path(__file__).parent
PPTX = TESTS_DIR / "test_deck.pptx"
PDF = TESTS_DIR / "test_doc.pdf"


# --- kebab_stem ---------------------------------------------------------------


def test_kebab_stem_basic():
    assert kebab_stem("03 What makes things fun to learn") == (
        "03-what-makes-things-fun-to-learn"
    )


def test_kebab_stem_folds_accents():
    assert kebab_stem("03 Ökologie & Präsentation") == "03-okologie-prasentation"


def test_kebab_stem_nordic_transliteration():
    assert kebab_stem("Østlandet Æ Ø Å") == "ostlandet-ae-o-a"


def test_kebab_stem_collapses_punctuation():
    assert kebab_stem("What's NEW_v2 (draft)") == "what-s-new-v2-draft"


def test_kebab_stem_keeps_non_latin_letters():
    assert kebab_stem("日本語 101") == "日本語-101"


def test_kebab_stem_caps_length_at_hyphen_boundary():
    name = " ".join(f"word{i:02d}" for i in range(40))
    stem = kebab_stem(name)
    assert len(stem) <= 100
    assert not stem.endswith("-")
    # cut at a hyphen boundary, not mid-word
    assert stem.split("-")[-1] == "word13" or stem.split("-")[-1].isdigit()


def test_kebab_stem_fallback_for_symbols_only():
    assert kebab_stem("  --  ") == "document"
    assert kebab_stem("") == "document"


def test_kebab_stem_is_idempotent():
    once = kebab_stem("03 Ökologie & Präsentation")
    assert kebab_stem(once) == once


# --- default_output_dir / is_archived ----------------------------------------


def test_default_output_dir_is_source_parent(tmp_path):
    src = tmp_path / "deck.pdf"
    assert default_output_dir(src) == tmp_path


def test_default_output_dir_for_archived_file(tmp_path):
    src = tmp_path / "source" / "deck.pdf"
    assert default_output_dir(src) == tmp_path
    assert is_archived(src)


def test_is_archived_case_insensitive(tmp_path):
    assert is_archived(tmp_path / "Source" / "deck.pdf")


# --- LayoutResolver -----------------------------------------------------------


def test_resolver_explicit_output_disables_organizing(tmp_path):
    layout = LayoutResolver(tmp_path / "out").resolve(tmp_path / "Deck One.pptx")
    assert layout.output_dir == tmp_path / "out"
    assert layout.stem == "Deck One"
    assert layout.source_dest is None


def test_resolver_default_organizes(tmp_path):
    src = tmp_path / "Deck One.pptx"
    src.write_bytes(b"")
    layout = LayoutResolver().resolve(src)
    assert layout.output_dir == tmp_path
    assert layout.stem == "deck-one"
    assert layout.source_dest == tmp_path / "source" / "deck-one.pptx"


def test_resolver_already_archived_is_not_moved(tmp_path):
    src = tmp_path / "source" / "Old Name.pdf"
    src.parent.mkdir()
    src.write_bytes(b"")
    layout = LayoutResolver().resolve(src)
    assert layout.output_dir == tmp_path
    assert layout.stem == "old-name"
    assert layout.source_dest is None


def test_resolver_batch_collision_gets_suffix_and_warning(tmp_path):
    a = tmp_path / "A B.pptx"
    a.write_bytes(b"")
    b = tmp_path / "a-b.pptx"
    b.write_bytes(b"")
    resolver = LayoutResolver()
    first = resolver.resolve(a)
    second = resolver.resolve(b)
    assert first.stem == "a-b"
    assert first.source_dest == tmp_path / "source" / "a-b.pptx"
    assert second.stem == "a-b-2"
    assert second.source_dest == tmp_path / "source" / "a-b-2.pptx"
    assert any("collision" in warning.lower() for warning in second.warnings)


def test_resolver_project_folder_named_source_is_left_in_place(tmp_path):
    project = tmp_path / "source"
    project.mkdir()
    (project / "main.tex").write_text("x")
    layout = LayoutResolver().resolve(project)
    assert layout.output_dir == tmp_path
    assert layout.stem == "source"
    assert layout.source_dest is None
    assert any("named like the archive" in w.lower() for w in layout.warnings)


def test_resolver_callable_without_source_resolver_does_not_organize(tmp_path):
    src = tmp_path / "Deck One.pptx"
    src.write_bytes(b"")
    resolver = LayoutResolver(lambda path: tmp_path / "out")
    layout = resolver.resolve(src)
    assert layout.output_dir == tmp_path / "out"
    assert layout.stem == "Deck One"
    assert layout.source_dest is None


def test_resolver_source_resolver_maps_to_original(tmp_path):
    real = tmp_path / "papers" / "Deck One.pdf"
    real.parent.mkdir()
    real.write_bytes(b"")
    staged = tmp_path / "uploads" / "Deck One.pdf"
    staged.parent.mkdir()
    staged.write_bytes(b"")
    resolver = LayoutResolver(
        lambda path: real.parent, source_resolver=lambda path: real
    )
    layout = resolver.resolve(staged)
    assert layout.output_dir == real.parent
    assert layout.stem == "deck-one"
    assert layout.source_dest == real.parent / "source" / "deck-one.pdf"


def test_resolver_source_resolver_none_disables_organizing(tmp_path):
    staged = tmp_path / "uploads" / "Deck One.pdf"
    staged.parent.mkdir()
    staged.write_bytes(b"")
    resolver = LayoutResolver(
        lambda path: staged.parent / "markdown",
        source_resolver=lambda path: None,
    )
    layout = resolver.resolve(staged)
    assert layout.output_dir == staged.parent / "markdown"
    assert layout.stem == "Deck One"
    assert layout.source_dest is None


# --- archive_source -----------------------------------------------------------


def test_archive_source_moves_file(tmp_path):
    source = tmp_path / "deck.pdf"
    source.write_bytes(b"new")
    dest = tmp_path / "source" / "deck.pdf"
    assert archive_source(source, dest, []) == dest
    assert dest.read_bytes() == b"new"
    assert not source.exists()


def test_archive_source_replaces_existing(tmp_path):
    source = tmp_path / "deck.pdf"
    source.write_bytes(b"new")
    dest = tmp_path / "source" / "deck.pdf"
    dest.parent.mkdir()
    dest.write_bytes(b"old")
    warnings: list[str] = []
    assert archive_source(source, dest, warnings) == dest
    assert dest.read_bytes() == b"new"
    assert not source.exists()
    assert not (tmp_path / "source" / ".deck.pdf.ptm-backup").exists()


def test_archive_source_moves_directory(tmp_path):
    project = tmp_path / "thesis"
    project.mkdir()
    (project / "main.tex").write_text("x")
    dest = tmp_path / "source" / "thesis"
    assert archive_source(project, dest, []) == dest
    assert (dest / "main.tex").exists()
    assert not project.exists()


def test_archive_source_restores_existing_on_failure(tmp_path):
    missing = tmp_path / "missing.pdf"
    dest = tmp_path / "source" / "deck.pdf"
    dest.parent.mkdir()
    dest.write_bytes(b"old")
    warnings: list[str] = []
    assert archive_source(missing, dest, warnings) is None
    assert dest.read_bytes() == b"old"
    assert warnings


# --- collect_inputs -----------------------------------------------------------


def test_collect_inputs_skips_source_archives(tmp_path):
    (tmp_path / "deck.pptx").write_bytes(b"")
    archive = tmp_path / "source"
    archive.mkdir()
    (archive / "old.pdf").write_bytes(b"")
    assert collect_inputs([tmp_path]) == [tmp_path / "deck.pptx"]
    # An explicitly selected source/ folder is still scanned.
    assert collect_inputs([archive]) == [archive / "old.pdf"]


# --- end-to-end through convert_file/convert_files ----------------------------


def test_convert_file_default_layout_moves_source(tmp_path):
    src = tmp_path / "03 What makes things fun.pptx"
    shutil.copy(PPTX, src)
    result = convert_file(src)
    assert result.error is None
    assert result.md_path == tmp_path / "03-what-makes-things-fun.md"
    assert result.md_path.exists()
    assert result.moved_to == tmp_path / "source" / "03-what-makes-things-fun.pptx"
    assert result.moved_to.exists()
    assert not src.exists()


def test_rerun_from_archive_is_idempotent(tmp_path):
    src = tmp_path / "Deck.pptx"
    shutil.copy(PPTX, src)
    first = convert_file(src)
    assert first.moved_to is not None
    second = convert_file(first.moved_to)
    assert second.error is None
    assert second.md_path == first.md_path
    assert second.moved_to is None
    assert not (tmp_path / "source" / "source").exists()


def test_duplicate_mode_uses_kebab_stem(tmp_path):
    src = tmp_path / "Deck.pptx"
    shutil.copy(PPTX, src)
    first = convert_file(src, duplicate_if_exists=True)
    assert first.md_path == tmp_path / "deck.md"
    # Re-converting the archived source keeps the prior output (ADR-0015) and
    # names the new one from the kebab stem.
    second = convert_file(first.moved_to, duplicate_if_exists=True)
    assert second.md_path == tmp_path / "deck (2).md"
    assert first.md_path.exists()


def test_convert_files_dedups_normalized_collisions(tmp_path):
    a = tmp_path / "A B.pptx"
    b = tmp_path / "a-b.pptx"
    shutil.copy(PPTX, a)
    shutil.copy(PPTX, b)
    results = convert_files([a, b])
    assert [r.error for r in results] == [None, None]
    assert results[0].md_path == tmp_path / "a-b.md"
    assert results[1].md_path == tmp_path / "a-b-2.md"
    assert results[0].moved_to == tmp_path / "source" / "a-b.pptx"
    assert results[1].moved_to == tmp_path / "source" / "a-b-2.pptx"
    assert any("collision" in w.lower() for w in results[1].warnings)


def test_failed_conversion_leaves_source_in_place(tmp_path):
    src = tmp_path / "broken.pdf"
    src.write_bytes(b"not a pdf")
    result = convert_file(src)
    assert result.error is not None
    assert src.exists()
    assert result.moved_to is None


def test_explicit_output_never_compresses_or_moves(tmp_path, monkeypatch):
    src = tmp_path / "Paper.pdf"
    shutil.copy(PDF, src)
    calls: list[Path] = []
    monkeypatch.setattr(
        "converter.compress.compress_pdf",
        lambda path, warnings: calls.append(path) or True,
    )
    out = tmp_path / "out"
    result = convert_file(src, out)
    assert result.error is None
    assert src.exists()
    assert result.moved_to is None
    assert calls == []


def test_archived_pdf_is_compressed_after_move(tmp_path, monkeypatch):
    src = tmp_path / "Paper.pdf"
    shutil.copy(PDF, src)
    calls: list[Path] = []
    monkeypatch.setattr(
        "converter.compress.compress_pdf",
        lambda path, warnings: calls.append(path) or True,
    )
    result = convert_file(src)
    assert result.error is None
    assert result.moved_to == tmp_path / "source" / "paper.pdf"
    assert calls == [tmp_path / "source" / "paper.pdf"]


def test_pdf_compression_disabled_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PDF_COMPRESS", "off")
    assert config.pdf_compress_enabled() is False
    src = tmp_path / "Paper.pdf"
    shutil.copy(PDF, src)
    calls: list[Path] = []
    monkeypatch.setattr(
        "converter.compress.compress_pdf",
        lambda path, warnings: calls.append(path) or True,
    )
    result = convert_file(src)
    assert result.error is None
    assert calls == []
