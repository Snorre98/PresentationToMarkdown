# 0049. Best-effort PDF compression when sources are archived

- Status: Accepted
- Date: 2026-10-13

## Context

ADR-0048 moves every converted source into its folder's `source/` archive. That
archive is a long-lived copy — the note-taking workflow treats it as the
ground-truth document and rarely opens it again — so lecture PDFs (scanned
handouts, exported slide decks with oversized images) keep consuming
multi-megabyte budgets per folder, per semester.

The archive move is the natural compression moment: the file is being written
anyway, there is exactly one archive copy to keep canonical, and the original
bytes still exist in the source folder until the move — so a failed or
unhelpful compression can simply leave them alone.

Constraints:

- Compression must never break a conversion, never corrupt an archive, and
  never silently degrade the archive when the tool is missing or the result
  isn't valid.
- Ghostscript is the only mainstream tool that actually shrinks image-heavy
  PDFs (image downsampling); qpdf is lossless-only (stream recompression) but
  is often installed where Ghostscript is not. Both are optional binaries on
  this machine and elsewhere.
- Like `IMAGE_LINKS`/`PDF_MODE`, the toggle should be runtime-flippable from
  the GUI/engine without a restart.

## Decision

**After a PDF is moved into the `source/` archive, compress it in place,
best-effort, with Ghostscript; fall back to qpdf; keep the original whenever
the result is invalid, not smaller, or the tool is missing.** On by default,
with a disable toggle.

1. **New module `converter/compress.py`.** `compress_pdf(path, warnings)`
   writes a candidate to a temporary file in the same directory (so the final
   swap is a rename) and returns whether the archive was replaced:
   - Ghostscript: `gs -sDEVICE=pdfwrite -dPDFSETTINGS=/printer` — moderate
     300 dpi image downsampling. The preset is overridable via
     `PDF_COMPRESS_PRESET`; `GS_PATH`/`QPDF_PATH` override the binaries.
   - qpdf fallback: `--object-streams=generate --recompress-flate
     --compress-streams=y` (lossless, smaller gains) when `gs` is unavailable.
   - Sanity check before swapping: PyMuPDF opens the candidate and its page
     count equals the original's; the candidate must be strictly smaller.
   - Every failure mode appends a human-readable warning and returns `False`
     without touching the original bytes; a missing tool is a skip, not an
     error.
2. **Trigger.** Only on the ADR-0048 archive move:
   `converter/__init__._convert_one` calls the compressor after a successful
   `archive_source`, for `.pdf` targets only. Never with an explicit output
   folder, and never on re-runs of an already-archived file (no repeated
   recompression / generation loss).
3. **Toggle.** `config.pdf_compress_enabled()` (lazy env read, default on),
   `PDF_COMPRESS=off` / `ptm --no-compress` / desktop GUI checkbox / web UI
   checkbox (`/api/config`) / TUI settings row (`pdf_compress`). The value is
   captured in the per-run config snapshot.

## Consequences

- Archived PDFs shrink by default (Ghostscript `/printer`; observed ~15% on the
  synthetic test document, typically far more on image-heavy scans) with the
  original bytes preserved whenever anything goes wrong.
- Lossy image downsampling (300 dpi) means the archive is no longer a
  byte-identical copy: re-deriving page renders from it yields a lower-quality
  ground truth than the original had. Accepted — the original upload/download
  remains the user's to keep, and the moderate preset is deliberately not the
  aggressive `/ebook` (150 dpi).
- The compressor is an optional dependency: `brew install ghostscript qpdf`.
  Missing tools degrade to a warning on each archived PDF (deduplicated by the
  log, not by the code).
- PDFs archived while compression was off (or the tools were missing) are not
  retro-compressed by a later re-scan; compress them explicitly if wanted.

## Alternatives considered

- **Lossless-only (qpdf)** — zero quality loss but typically single-digit
  percentage gains, which does not address the space problem. The
  Ghostscript/qpdf chain gets the real wins and keeps the fallback.
- **Aggressive `/ebook` (150 dpi)** — saves more but visibly degrades scans and
  slide exports that the vision passes may later read; rejected as a default.
- **Always keep both original and compressed copy** — the point is to keep one
  canonical archive copy; keeping both doubles the footprint and reintroduces
  the "which file is real" ambiguity ADR-0048 removes.
- **Compress the source in place before conversion** — would degrade the
  conversion's own vision inputs and makes rollback impossible; rejected.
- **An opt-in flag (off by default)** — compression is safe-by-construction
  here (validated, smaller-only, failures keep the original), so the default
  should deliver the space win; the toggle exists for people who want
  byte-faithful archives.
