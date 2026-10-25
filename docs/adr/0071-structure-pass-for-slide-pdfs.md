# 0071. The structure pass covers slide PDFs

- Status: Accepted
- Date: 2026-10-25
- Relates to: [0011](0011-paper-structure-llm-pass.md),
  [0052](0052-paper-continuous-document.md),
  [0065](0065-ai-capability-labels-and-requirements.md),
  [0066](0066-ai-setup-control-surface.md),
  [0070](0070-slide-paragraphs-and-headings.md)

## Context

The optional document-structure pass (`converter/structure.py`) was built for
paper mode: it amends one paper page at a time, allows page 1's `# Title` to be
edited, forbids top-level headings on later pages, and reinserts a single blank
line between pages. Slide mode had no equivalent pass; its deterministic output
was the final word unless the Markdown-restructure pass accepted a page.

ADR-0070 gives slide mode the deterministic paragraph/heading model, but the
same residual failure class remains: a title recovered from the wrong display
run, diagram labels linearized in the wrong order, a missing `##` subheading, or
a wrapped line the deterministic segmenter split too eagerly. The paper
structure pass already has the expensive parts — the per-page text/image
regimes, the verbatim word gate, the need gate, logging and per-page
degradation — so duplicating those for slides would create two copies of the
same safety machinery.

Slide pages differ from paper pages in two ways that the paper contract cannot
express:

- **Every slide has a top-level `#` heading.** It is the per-page title plus the
  deterministic `— Page N` provenance suffix. The model may fix the title's
  Markdown, but it must not drop the suffix or add a second top-level heading.
- **Pages are separated by the slide scaffold.** The page-break `<div>` and the
  visible `---` rule are written by the converter between pages, outside the
  per-page `PageData`. A structure pass that rewrites a deck must reinsert that
  scaffold deterministically.

## Decision

Extend the existing structure pass to slide mode; do not add a second pass.

1. **Shared internals, mode switch.** `structure_paper` and the new
   `structure_slides` delegate to a shared `_structure_pages(pages, mode, …)`.
   The per-page regimes, verbatim gates, need gate, logging and failure
   degradation are unchanged. `structure_paper` keeps its byte-identical
   behavior.

2. **Slide text prompt.** The model sees the numbered body lines with layout
   metadata and the current Markdown. It may:
   - fix the slide title's Markdown (for example remove a stray bullet glyph or
     move a subtitle into the body) while keeping every title word;
   - add `##`/`###` subheadings and keep existing heading levels;
   - rejoin wrapped lines, fix list nesting and reorder diagram/text-box labels;
   - keep bullets, page links, tables, blockquotes, `<details>` blocks, fenced
     code and horizontal rules byte-exact.

   The title is the only amendment target among top-level headings. Every other
   structural line is an anchor.

3. **Slide title invariant.** The reply must contain exactly one `# ` heading
   outside fenced code, and it must end with the exact ` — Page N` suffix (or be
   the `# Page N` fallback). A lost suffix, a wrong page number or a second
   top-level heading rejects the page and keeps the deterministic Markdown with
   a warning. The check is fence-aware for both modes.

4. **Image regime.** Slides with no usable text layer are reworded from the
   rendered page image with a slide prompt that asks for `# Slide title` but no
   page number. A deterministic canonicaliser appends the ` — Page N` suffix
   (stripping a model-supplied page suffix first) or falls back to `# Page N`
   when the model returned no title. Image readability, transcription quality
   and page-link gates are unchanged.

5. **Page scaffold.** `PageData` stays page-local: no page-break div or `---`
   rule. `_structure_pages(..., mode="slides")` reinserts the writer's exact
   `"" + page-break div + "" + --- + ""` sequence after every page, including
   the last page, so a changed deck is byte-consistent with the deterministic
   writer. If no page changed, the pass returns `None` and the caller keeps the
   deterministic lines.

6. **Need gate.** `needs_restructure(..., mode="slides")` uses a slide-specific
   prompt: interleaved diagram/text-box labels, missing `##` subheadings, a
   slide title to fix, or wrapped lines to rejoin. `NEED_GATE` behavior is
   unchanged.

7. **Heading words.** Slide subheadings may add a small set of structural
   words (agenda, overview, summary, takeaways, recap, questions, details, …)
   that are not paper-section vocabulary. The word gate still requires every
   body word to be verbatim; paper mode keeps the narrower paper set.

8. **Feature surface.** `structure` is now a PDF pass for both modes:
   - label **PDF structure** (was *Paper structure*);
   - scope `all` (was `paper`), so the AI setup screen no longer marks it
     paper-only or warns when paper mode is off;
   - the AI setup's non-paper group is **Document passes**;
   - `--structure` and `STRUCTURE_ENABLED=1` apply to slide decks;
   - the existing `STRUCTURE_*` / `STRUCTURE_TEXT_*` model configuration is
     reused; no new server or env var.

## Consequences

- Slide decks gain an optional AI structure pass without a second code path or
  a second model configuration.
- The pass is still off by default. When enabled it costs one model call per
  text page (image pages one call each), reduced by the need gate; a long deck
  can therefore be expensive and currently emits no per-page progress (the
  existing structure-pass progress gap).
- A title amendment that removes words the model cannot ground is rejected;
  the page keeps its deterministic title. This is intentional: slide titles are
  often short, and a hallucinated title is worse than a missed fix.
- When any page is amended, the whole deck is rewritten with the deterministic
  page-break scaffold and the output is marked as model-produced (ADR-0050).
  A page whose reply is byte-identical after whitespace normalisation is not
  counted as amended.
- Paper mode's structure pass output and prompts are unchanged.
- The TUI/CLI/docs no longer describe the structure pass as paper-only.

## Alternatives considered

- **Add a separate slide-structure pass and prompt/model.** Duplicates the
  regimes, word gate, need gate and logging; two places to fix. Rejected.
- **Reuse the paper prompt unchanged.** It forbids `#` headings on later pages
  and edits page 1's title only; every slide page after the first would be
  rejected as a heading-invariant violation. Rejected.
- **Let the model own the `— Page N` suffix.** It would drift, drop page
  numbers and break the per-slide chunking/provenance. The suffix is
  deterministic. Rejected.
- **Keep the pass paper-only.** The deterministic slide model already fixes
  most body issues, but the residual diagram/title/subheading failures remain;
  the user-facing request was to bring the paper pass's quality to slides.
  Rejected.
- **Reconstruct the page scaffold from the model's output.** The model never
  sees it and could reorder or drop it; reinserting it deterministically is
  exact. Rejected.
