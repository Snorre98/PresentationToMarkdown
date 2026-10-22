"""PDF -> EPUB 3 packaging for a converted Markdown document (ADR-0067).

The PDF converter remains the only extraction backend. This module takes the
Markdown it just wrote and packages it with ``pandoc`` into an EPUB that sits
beside it (``foo.md`` -> ``foo.epub``), so the EPUB inherits the converter's
headings, paragraphs, tables, figures and math.

The export is *additive*: the Markdown is never deleted, and a packaging
failure (missing pandoc, timeout, malformed document) only appends a warning.
The EPUB is named from the final Markdown path, so the ``-inference-used``
marker propagates (ADR-0050).

The EPUB is reflowable, while the source is paginated. Page numbers, footnote
anchors and wide tables lose some of their layout; the document's text,
figure images and display math are preserved.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from converter import config

PANDOC_PATH = os.environ.get("PANDOC_PATH", "pandoc")
PANDOC_TIMEOUT = 300

# Slide mode separates pages with a raw HTML page-break div and a rule; both
# are meaningless (and noisy) in a reflowable EPUB.
_PAGE_BREAK = '<div style="page-break-after: always; break-after: page;"></div>'
_PAGE_BREAK_PAIR_RE = re.compile(
    rf"^{re.escape(_PAGE_BREAK)}[ \t]*\n\s*---[ \t]*\n", re.MULTILINE
)
_PAGE_BREAK_RE = re.compile(rf"^{re.escape(_PAGE_BREAK)}[ \t]*$", re.MULTILINE)

# The converter's page-render and recurring-image links. Pandoc keeps these as
# relative hyperlinks, which are broken inside an EPUB (the render is not
# packaged); figure images use `![...]` and must stay.
_ASSET_ONLY_LINK_RE = re.compile(
    r"^(?:\[Page\s+\d+\]|\[image\])\(assets/[^)]*\)[ \t]*$"
)

_HEADING_RE = re.compile(r"^#\s+(.+?)\s*$")
_ITALIC_RE = re.compile(r"^\*(.+)\*$")

# Paper mode's title is the first real top-level heading (the RAG summary, when
# enabled, prepends `# Summary` / `## Abstract`, which are not the document
# title). Only scan the document's opening; sections use `##`.
_TITLE_SCAN_LIMIT = 120
_SKIP_TITLES = {"summary", "abstract", "contents", "table of contents"}


def pandoc_available() -> bool:
    """Whether the external ``pandoc`` binary can be found."""
    if os.path.isabs(PANDOC_PATH):
        return Path(PANDOC_PATH).exists()
    return shutil.which(PANDOC_PATH) is not None


def build_epub(
    md_path: Path, source_pdf: Path, warnings: list[str]
) -> Path | None:
    """Package ``md_path`` as ``<stem>.epub``; return the path or ``None``.

    ``source_pdf`` supplies the title/author/language metadata and the cover
    (page 1). Every failure is appended to ``warnings`` and never raises; the
    Markdown conversion itself stays successful.
    """
    if not pandoc_available():
        warnings.append(
            "EPUB export needs pandoc (install it: brew install pandoc)"
        )
        return None
    try:
        original = md_path.read_text(encoding="utf-8")
    except OSError as exc:
        warnings.append(f"EPUB export could not read the Markdown: {exc}")
        return None

    paper = os.environ.get("PDF_MODE", "").strip().lower() == "paper"
    body = _strip_page_breaks(original)
    title, author, language, remove_index = _metadata(
        md_path, source_pdf, body.splitlines(), paper
    )
    body = _strip_title_and_links(body, remove_index, author)

    final = md_path.with_suffix(".epub")
    temp_md: Path | None = None
    temp_epub: Path | None = None
    cover_dir: str | None = None
    try:
        temp_md = _temp_path(md_path.parent, ".md")
        temp_md.write_text(body, encoding="utf-8")
        temp_epub = _temp_path(md_path.parent, ".epub")

        cmd = [
            PANDOC_PATH,
            str(temp_md),
            "-f",
            "gfm+tex_math_dollars+pipe_tables",
            "-t",
            "epub3",
            "-o",
            str(temp_epub),
            "--toc",
            "--toc-depth=3",
            f"--split-level={2 if paper else 1}",
            f"--resource-path={md_path.parent}",
            "--metadata",
            f"title={title}",
            "--metadata",
            f"identifier=urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, md_path.stem)}",
        ]
        if author:
            cmd += ["--metadata", f"author={author}"]
        if language:
            cmd += ["--metadata", f"lang={language}"]

        if config.epub_cover_enabled():
            cover_dir = tempfile.mkdtemp(prefix="ptm-epub-cover-")
            cover = Path(cover_dir) / "cover.png"
            if _write_cover(source_pdf, cover):
                cmd += ["--epub-cover-image", str(cover)]

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=PANDOC_TIMEOUT,
            cwd=str(md_path.parent),
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            message = detail[0][:300] if detail else f"pandoc exited {proc.returncode}"
            warnings.append(f"EPUB export failed: {message}")
            return None
        if not temp_epub.exists() or temp_epub.stat().st_size == 0:
            warnings.append("EPUB export failed: pandoc produced no output")
            return None
        if proc.stderr.strip():
            warnings.append(f"pandoc: {proc.stderr.strip().splitlines()[0]}")
        os.replace(temp_epub, final)
        return final
    except FileNotFoundError:
        warnings.append(
            "EPUB export needs pandoc (install it: brew install pandoc)"
        )
        return None
    except subprocess.TimeoutExpired:
        warnings.append("EPUB export timed out")
        return None
    except Exception as exc:  # noqa: BLE001 - packaging never fails a conversion
        warnings.append(f"EPUB export failed: {exc}")
        return None
    finally:
        if temp_md is not None:
            temp_md.unlink(missing_ok=True)
        if temp_epub is not None:
            temp_epub.unlink(missing_ok=True)
        if cover_dir is not None:
            shutil.rmtree(cover_dir, ignore_errors=True)


def _temp_path(directory: Path, suffix: str) -> Path:
    """Create a closed, non-existent temp path in ``directory`` for a build."""
    handle, name = tempfile.mkstemp(prefix=".ptm-epub-", suffix=suffix, dir=directory)
    os.close(handle)
    path = Path(name)
    path.unlink()
    return path


def _strip_page_breaks(text: str) -> str:
    """Remove slide-mode page-break divs and the rule that follows each."""
    text = _PAGE_BREAK_PAIR_RE.sub("", text)
    return _PAGE_BREAK_RE.sub("", text)


def _metadata(
    md_path: Path, source_pdf: Path, lines: list[str], paper: bool
) -> tuple[str, str, str, int | None]:
    """Return ``(title, author, language, redundant title heading index)``.

    PDF metadata wins over the Markdown heading (the PDF's own title is the
    most faithful); the heading is the fallback for paper-mode documents whose
    producer left the metadata empty. Slide mode's first heading is a slide
    title (``… — Page 1``), never the document title.
    """
    info = _pdf_metadata(source_pdf)
    heading, index = _first_title_heading(lines) if paper else ("", None)
    pdf_title = info.get("title", "")
    # A producer that copies the filename into the metadata title should not
    # beat a real title heading from the document body.
    if pdf_title.strip().lower() in {
        "",
        "untitled",
        md_path.stem.lower(),
        source_pdf.stem.lower(),
    }:
        pdf_title = ""
    title = pdf_title or heading or source_pdf.stem
    author = info.get("author", "")
    if paper and not author and index is not None:
        # The paper title block emits the authors as the first italic line
        # after the title; use it when the PDF metadata has none.
        probe = index + 1
        while probe < len(lines) and not lines[probe].strip():
            probe += 1
        if probe < len(lines):
            match = _ITALIC_RE.match(lines[probe].strip())
            if match:
                author = match.group(1).strip()
    language = config.epub_language() or info.get("lang") or "en"
    # Only drop the body heading when it is the very title shown on the
    # metadata title page; otherwise the document has its own title heading.
    remove_index = index if index is not None and heading == title else None
    return title, author, language, remove_index


def _first_title_heading(lines: list[str]) -> tuple[str, int | None]:
    """Return the first real ``# `` heading and its line index, if any."""
    for index, line in enumerate(lines[:_TITLE_SCAN_LIMIT]):
        match = _HEADING_RE.match(line.strip())
        if not match:
            continue
        text = match.group(1).strip()
        if text.lower() in _SKIP_TITLES:
            continue
        return text, index
    return "", None


def _strip_title_and_links(
    text: str, remove_index: int | None, author: str
) -> str:
    """Drop the redundant title/authors block and non-image asset links."""
    lines = text.splitlines()
    out: list[str] = []
    index = 0
    while index < len(lines):
        if index == remove_index:
            index += 1
            # The paper title block emits the authors as the next paragraph;
            # the metadata title page already shows them.
            probe = index
            while probe < len(lines) and not lines[probe].strip():
                probe += 1
            if author and probe < len(lines):
                match = _ITALIC_RE.match(lines[probe].strip())
                if match and match.group(1).strip() == author:
                    index = probe + 1
            continue
        if _ASSET_ONLY_LINK_RE.match(lines[index].strip()):
            index += 1
            continue
        out.append(lines[index])
        index += 1
    return _collapse_blank_lines(out)


def _collapse_blank_lines(lines: list[str]) -> str:
    """Join the body, keeping at most two consecutive blank lines."""
    cleaned: list[str] = []
    blanks = 0
    for line in lines:
        if line.strip():
            blanks = 0
            cleaned.append(line)
        else:
            blanks += 1
            if blanks <= 2:
                cleaned.append("")
    return "\n".join(cleaned).rstrip() + "\n"


def _pdf_metadata(source_pdf: Path) -> dict[str, str]:
    """Read the PDF's title/author/catalog language; never raises."""
    try:
        import pymupdf as fitz
    except Exception:  # pragma: no cover - PyMuPDF is a hard dependency
        return {}
    try:
        doc = fitz.open(source_pdf)
    except Exception:
        return {}
    try:
        raw = doc.metadata or {}
        data = {
            "title": (raw.get("title") or "").strip(),
            "author": (raw.get("author") or "").strip(),
        }
        try:
            kind, value = doc.xref_get_key(doc.pdf_catalog(), "Lang")
            if kind == "string" and value:
                data["lang"] = value.strip("()")
        except Exception:
            pass
        return data
    finally:
        doc.close()


def _write_cover(source_pdf: Path, target: Path) -> bool:
    """Render page 1 as the EPUB cover; best-effort."""
    try:
        import pymupdf as fitz
    except Exception:  # pragma: no cover - PyMuPDF is a hard dependency
        return False
    try:
        doc = fitz.open(source_pdf)
    except Exception:
        return False
    try:
        if doc.page_count < 1:
            return False
        pix = doc[0].get_pixmap(matrix=fitz.Matrix(2, 2))
        pix.save(str(target))
        return True
    except Exception:
        return False
    finally:
        doc.close()
