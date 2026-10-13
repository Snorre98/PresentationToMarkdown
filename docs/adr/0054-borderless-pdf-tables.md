# 0054. Borderless PDF tables reconstructed from the text-layer stream

- Status: Accepted
- Date: 2026-10-13

## Context

A PDF table is not a document feature: it is glyphs plus (sometimes) ruling
lines. `converter/pdf.py` has always delegated table detection to PyMuPDF's
`page.find_tables()`, whose default strategy keys on ruling lines. Word — and
comparable layout engines — can export a table with **no ruling lines at all**:
the grid is implied by whitespace alone. On such pages `find_tables()` returns
nothing, the cell fragments fall through to the `(row, x)` line pipeline, and
the table converts to interleaved fragments (the motivating assignment,
`problemd-assign-2.pdf` pages 2–5, whose four columns scrambled into one column
of half-sentences; the same authoring style also carries tables in
`taskdescription-assignment2.pdf`).

Finding the table region is the easy half. The hard half is rebuilding rows: on
page 2 of the fixture the two adjacent single-line rows ("Previous
repairs/faults", "Warning codes") are 17.1pt apart — exactly the 17.1pt pitch
of the wrapped lines *inside* the tall neighbour cell — and every cell is
vertically centred, so no gap, height or alignment threshold can separate a row
boundary from a wrapped line. The one remaining deterministic signal is the
page's **content-stream order**: `Page.get_texttrace()` returns spans in the
order they were written to the page, and Word writes table cells row by row
(with a cell's wrapped lines inside a single span). A row boundary is a
leftward jump of the cell column in that stream.

## Decision

Add `converter/borderless.py`, a deterministic text-layer detector, and feed its
tables through the existing table plumbing.

- Spans are read with `Page.get_texttrace()` (visible text only, in stream
  order); a span's wrapped lines are recovered from character origins and
  joined.
- A span's y-band assigns it to the current row; when the band no longer
  overlaps, a new row opens. The stream's leftward column jump — not the
  geometry — is what separates the equal-pitch rows above. A cell that starts
  mid-line (a fragment or an inline dash) continues the previous cell, and
  consecutive same-column spans are folded into one cell for producers that
  write one span per text line.
- A run qualifies as a table only under conservative gates: at least three
  rows; no cell wider than 60% of the page; bullet and `N.`/`a.` marker rows
  break the run (lists are not tables); at least two rows with two or more
  non-empty cells; and 2–6 columns cut from the run's global empty x-intervals,
  so a centred or wide header cell cannot merge two columns while a run whose
  fragments cover every gutter (prose) is rejected outright.
- The result is a `BorderlessTable` exposing `bbox` and `extract()` — the same
  surface as a PyMuPDF `Table` — so `_line_in_tables`, `_tables_for_columns`,
  `_with_table_flow` and the vision pass consume it unchanged. Ruled tables
  keep precedence: a borderless candidate mostly inside a ruled table is
  dropped, so a bordered table is never emitted twice.
- Detection is **on by default**; `PDF_BORDERLESS_TABLES=off`, read lazily at
  conversion time like `IMAGE_LINKS`/`PDF_COMPRESS` (ADR-0012's exception
  pattern), disables it and restores the old line-based output.
  `config.snapshot()` reports the preference.
- The table emitter now separates a table from a preceding paragraph with a
  blank line (a pipe table is a leaf block). This also applies to ruled tables.

## Consequences

- Word-layout-grid PDFs convert to correct pipe tables: the motivating
  fixture's page-2 table matches its ground-truth text exactly, pages 3–5 all
  convert, and the sibling document's criterion and grade tables convert too.
- Zero new dependencies and no AI pass: detection uses the PyMuPDF text layer
  already required for layout reconstruction, so the deterministic path never
  needs a model, and repeated runs are byte-identical.
- Fewer pages trip `_page_is_complex`: with the table fragments excluded, a
  table page's remaining text is a single column, so the deterministic path is
  used even when the vision feature is enabled.
- Conservative by design: a 2-row borderless table, a table with a page-wide
  cell, or gutters narrower than 4pt stay text — today's behaviour — rather
  than risk turning prose into a table. A table split across a page boundary is
  not reassembled.
- Detection relies on the producer's stream order matching the row-major cell
  pattern (Word and the common engines do). A page whose stream does not match
  yields no candidate and keeps today's output — never a guessed table.
- Excluding table fragments from a page's line set can change paper-mode
  heading promotion for a wrapped bold paragraph fragment on a table page;
  the heading heuristics themselves are unchanged here.

## Alternatives considered

- **img2table** (`borderless_tables=True`, opencv/pypdfium2) — detected five of
  the fixture's six tables with mostly correct cells, but emitted spill rows
  for tall cells, split some headers, adds a heavy binary dependency stack to a
  pure-Python converter, and works in rendered-pixel space. Rejected.
- **markitdown** — emits pipe tables for PDFs, but on the motivating table
  produced interleaved fragments and a half-empty row; a new dependency for a
  problem it does not solve. Rejected (kept as a candidate for a separate
  non-PDF-engine evaluation).
- **Docling/TableFormer and Table Transformer** — ML table models; Docling has
  an open issue for borderless whitespace-aligned tables, and weights/torch
  dependencies conflict with the deterministic, offline converter core (the
  deprecated ADR-0046 already rejected model-based PDF producers for the
  default path). Rejected.
- **Camelot (stream flavour)** — opencv-based, untested on the fixtures, same
  dependency and accuracy trade-offs as img2table. Rejected.
- **PyMuPDF `find_tables(strategy="text")`** — the built-in fallback; on the
  fixture it produced a bogus whole-page table and split words at column
  boundaries even when clipped to the true region. Rejected.
- **Detect only, without row reconstruction** (emit the region as raw text) —
  no benefit over today's output; the values are the rows. Rejected.
