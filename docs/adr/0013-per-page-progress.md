# 0013. Per-page progress reporting

- Status: Accepted
- Date: 2026-09-02

## Context

Progress reporting is file-granular. `ProgressCallback` (`Callable[[int, int,
str], None]`, `converter/base.py`) fires once per *file* from `convert_files`
after the whole document has been converted (`converter/__init__.py`). The GUI's
single `QProgressBar` is maxed to the number of files, so a long deck or PDF
shows no movement until the file completes; with the opt-in AI passes
(vision/classify/structure) a single page can take many seconds, which reads as a
hang. The CLI prints one `[N/M] name` line per file.

Both converters already iterate their pages internally — PPTX over
`prs.slides` (`pptx.py`), PDF over `doc` (`pdf.py`) — but expose no hook, so the
per-page signal that already exists inside the loop is discarded.

## Decision

Add page-granular progress alongside the existing file-level callback, keeping
the change backward-compatible.

### Library

- `converter/base.py` gains `PageProgressCallback = Callable[[int, int, str],
  None]` — `(page, page_total, name)`. `Converter.convert` accepts an optional
  `progress_callback: PageProgressCallback | None = None` parameter.
- `convert_file` and `convert_files` accept and forward an optional
  `page_progress_callback`; the existing file-level `progress_callback` is
  unchanged.
- `PPTXConverter.convert` emits `(idx, len(prs.slides), path.name)` once per
  slide; `PDFConverter.convert` emits `(pno, doc.page_count, path.name)` once
  per page. Both emit only in the **main emission loop**, not the pre-scan
  passes (image-digest collection, footer/chart detection), which are fast
  metadata scans that would double-count pages.

### GUI

A second `QProgressBar` sits below the existing file-level bar and shows the
current document's `Slide/Page N of M`. The worker thread forwards page events
over a new `page_progressed` signal; the bar is shown/hidden with the file bar
and reset when a document completes. File-level log lines are unchanged (no
per-page log spam).

### CLI

`_page_progress` writes a carriage-return status line
`\r{name}: Slide {page}/{page_total}` **only when stderr is a TTY**; on a pipe
it is suppressed so scripts don't accumulate thousands of lines. `--quiet`
disables both callbacks as today. The noun (`Slide` vs `Page`) is derived from
the file extension by the UI layer, not baked into the callback.

## Consequences

- Long documents give continuous feedback at the granularity where the cost is
  (the per-page AI passes), not per file.
- Fully backward compatible: every new parameter is optional and defaults to
  `None`; deterministic conversion is unaffected (the callback never changes
  output).
- ADR-0003's CLI/GUI parity contract is preserved: both surfaces now report the
  same page-granular signal, in addition to the existing per-file lines.
- The pre-scan passes remain unreported; for a file that spends most of its time
  in pre-scan (rare — image-heavy PDFs) the page bar simply starts after a short
  delay.

## Alternatives considered

- **A single combined progress bar** (file index × page fraction) — needs each
  document's page count up front, which requires a second open/scan; rejected.
- **Pre-scan page counts for all files first** — enables an exact global
  denominator but doubles I/O and complicates error handling; rejected.
- **Reuse the file `ProgressCallback` type** for pages — zero new types, but
  `(idx, total, name)` is ambiguous between "file" and "page"; a named alias
  documents intent. Rejected.
- **A richer dataclass progress event** (file + page + phase in one) — more
  future-proof but overkill for the current two-level model; deferred.
- **Per-page log lines in the GUI/CLI** — rejected; floods the log for large
  decks. Progress bars / a single TTY status line carry the same information.
