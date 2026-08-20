"""PDF -> Markdown converter built on PyMuPDF."""
from __future__ import annotations

from pathlib import Path

import pymupdf as fitz

from converter.base import (
    ConvertResult,
    Converter,
    _format_md,
    write_image,
)

_BOLD_FLAG = 2**4
_ITALIC_FLAG = 2**1


def _spans_to_md(spans: list[dict]) -> str:
    """Join text spans, preserving bold/italic via span flags."""
    return "".join(
        _format_md(
            span["text"],
            bool(span["flags"] & _BOLD_FLAG),
            bool(span["flags"] & _ITALIC_FLAG),
        )
        for span in spans
    )


def _blocks_to_md(blocks: list[dict]) -> list[str]:
    """Turn text blocks into markdown lines with paragraph separation."""
    out: list[str] = []
    pending_blank = False
    for block in blocks:
        if block.get("type") != 0:  # only text blocks
            continue
        for line in block.get("lines", []):
            text = _spans_to_md(line.get("spans", [])).strip()
            if text:
                if pending_blank:
                    out.append("")
                    pending_blank = False
                out.append(text)
            elif not pending_blank:
                pending_blank = True
    return out


def _extract_images(page, doc, assets_dir: Path, stem: str, counter: list[int], warnings: list[str]) -> list[str]:
    lines: list[str] = []
    for image in page.get_images(full=True):
        xref = image[0]
        try:
            extracted = doc.extract_image(xref)
        except Exception as exc:
            warnings.append(f"Could not extract image: {exc}")
            continue
        filename = write_image(
            extracted["image"],
            extracted.get("ext", "bin"),
            assets_dir,
            stem,
            counter,
            warnings,
        )
        if filename:
            rel = f"assets/{stem}/{filename}"
            lines.append(f"![image]({rel})")
    return lines


class PDFConverter(Converter):
    extensions = (".pdf",)

    def convert(self, path: Path, output_dir: Path) -> ConvertResult:
        result = ConvertResult(source_path=path)
        try:
            doc = fitz.open(path)
            stem = path.stem
            assets_dir = output_dir / "assets" / stem
            lines: list[str] = []
            counter = [1]
            for page_num, page in enumerate(doc, start=1):
                lines.append(f"# Page {page_num}")
                lines.append("")
                if page.get_images(full=True):
                    assets_dir.mkdir(parents=True, exist_ok=True)
                    lines.extend(_extract_images(page, doc, assets_dir, stem, counter, result.warnings))
                    lines.append("")
                lines.extend(_blocks_to_md(page.get_text("dict").get("blocks", [])))
                lines.append("")
            doc.close()
            md_path = output_dir / f"{stem}.md"
            md_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
            result.md_path = md_path
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        return result
