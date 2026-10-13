# 0052. Paper mode is one continuous document (no per-page headings)

- Status: Accepted
- Date: 2026-10-13

## Context

Paper mode (`PDF_MODE=paper`) converted a PDF into Markdown with one top-level
heading per physical page: page 1's `# Title` (or a `# Page 1` fallback) and
`# Page N` on every later page, with the paper's real section headings as `##`
between them. That treats each PDF page as a Markdown "slide".

The pages of real papers are not slides. A journal/A4/Letter page — often
taller than the slide canvas the convention was borrowed from — holds a large
amount of prose, and a paper's sections routinely start mid-page and continue
across one or more page boundaries. The per-page `# Page N` headings therefore
slice sections apart: the tail of a section continues on the next page under a
new `# Page N` heading instead of under the `##` section it belongs to, and a
Markdown outline of the paper is a list of page numbers rather than the paper's
own sections.

The convention also leaked into three passes:

- The structure pass (ADR-0011) required exactly one `# ` heading per page and
  treated each later page's `# Page N` as a byte-exact anchor; the image regime
  was instructed to emit one.
- The summary/RAG pass (`summary._iter_slides`, ADR-0021) chunks at top-level
  `# `, so each page was one chunk and `## Metadata` reported "Slides: N".
- The opt-in LLM format pass (ADR-0039) also chunks at `# ` and its prompt
  talks about a "lecture slide".

A converter whose Markdown is *one* document (LaTeX, ADR-0038: `# Title` +
`##` sections) already suffered the summary side effect of becoming a single
unchunked RAG blob, because the chunking contract was hard-coded to slides.

## Decision

**Paper-mode Markdown is one continuous document: no per-page headings.** Page 1
carries `# Title` + `*Authors*` when the title block is detected (and no heading
otherwise — no invented `# Page 1`); every later page begins directly with body
text or a `##` section heading. Content that continues across a page boundary
therefore stays under the paper's last real `##` section. Running
headers/footers are still stripped, pages still flow with no separators, and
the opt-in `[Page N](...)` image links (ADR-0047) are kept as provenance links.

The passes follow:

1. **Structure pass (ADR-0011).** The per-page contract becomes: page 1 may
   carry at most one `# ` line (the amendable title; the model may add a
   grounded one when the deterministic pass found none), and pages 2+ must
   carry none — a reply that adds one is rejected as a heading-invariant
   violation. The `# Page N` anchor disappears; `##` headings, tables, links
   and `<details>` blocks remain anchors. The image regime is reworded
   accordingly (page 1: `# Title`; later pages: `##` sections and body only).

2. **Chunking becomes a converter declaration.** `Converter.chunking` is
   `"slides"` (top-level `#`; the status quo) or `"sections"` (one continuous
   document split at `##`, preamble first). `PDFConverter` returns `"sections"`
   in paper mode and `"slides"` otherwise; `LatexConverter` declares
   `"sections"`. `convert_file` passes it to both post-passes.

3. **Format pass (ADR-0039).** In sections mode the LLM restructure runs once
   per `##` section (and once for the preamble) with a conservative paper
   prompt: rejoin wrapped lines and fix list nesting only, keep every heading,
   link, table, blockquote and `<details>` block byte-exact, never add or
   promote a heading (the structure pass owns headings). The pre-flight need
   gate gains a matching sections prompt. The deterministic whitespace pass is
   unchanged.

4. **Summary/RAG pass (ADR-0021).** Sections mode indexes one chunk per `##`
   section (the preamble chunk is titled by the `# Title`), retrieves and
   prompts with "section" wording, and reports `Sections: N` in `## Metadata`
   — for paper-mode PDFs and LaTeX projects alike.

## Consequences

- A converted paper's Markdown outline is the paper's own structure (title +
  `##` sections), not a page-number list; content on later pages sits under the
  section it belongs to.
- The structure pass's old per-page page-heading invariant is replaced; the
  rest of ADR-0011 stands (verbatim word gate, additive `##` headings, image
  regime, per-page failure locality).
- The summary RAG index for papers/LaTeX keeps per-section granularity instead
  of collapsing to one chunk; re-converting a paper re-indexes it because the
  section chunk boundaries and content hashes differ from the old page chunks.
- Determinism is unchanged: dropping the page headings and the section
  chunking are deterministic; only model output stays model output.
- Slide mode, PPTX output and LaTeX Markdown are unchanged; LaTeX only gains
  section-aware summary chunking.
- A real section heading the geometry heuristics miss keeps its content under
  the previous `##` until the structure pass adds it — the same failure mode as
  before, but without a page heading masking it.

## Alternatives considered

- **Keep `# Page N`, restyle it.** No downstream churn, but the outline problem
  and the section-splitting remain; rejected as precisely what this ADR
  removes.
- **Invisible `<!-- Page N -->` markers.** Preserves per-page provenance and
  gives the passes a boundary, but adds an output artifact whose only consumer
  is the passes themselves — and ADR-0050's ledger already records which pages
  were handled by which pass. Rejected.
- **Demote page headings to `###`.** Removes the outline noise, but a `###`
  under the last `##` still interrupts the section content, and the passes
  still need the chunking change. Rejected.
- **Auto-detect sections in the summary pass** (single `# ` ⇒ chunk by `##`).
  No plumbing, but implicit and wrong for edge shapes; the converter already
  knows its layout, so it declares it. Rejected.
- **Skip the LLM format pass in paper mode** (as LaTeX skips all format AI).
  Simpler, but loses wrapped-line reflow for papers converted without the
  structure pass. Rejected in favour of the conservative section pass.
