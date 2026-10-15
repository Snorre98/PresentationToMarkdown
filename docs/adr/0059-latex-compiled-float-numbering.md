# 0059. LaTeX float numbering matches the compiled document

- Status: Accepted (amends [0056](0056-latex-title-and-crossrefs.md))
- Date: 2026-10-15

## Context

ADR-0056 numbers floats with one linear scan of the source, from the same
index that produces the reference text, so captions and `\autoref` always
agree. The scan ran on the text handed to pandoc — after `_strip_frontmatter`
had removed the title page — and ADR-0056 recorded the consequence as a known
limitation: "the stripped title page consumed counter values in the compiled
PDF (the members table is Table 1 there), so a later table can be one lower in
the Markdown than in the PDF".

On the TDT4259 report that is exactly what happened: the compiled document's
`main.lot` lists Table 1 Members, Table 2 Business KPIs, Table 3 Model metrics,
Table 4 Stated confidence, while the Markdown numbered the content tables 1–3.
The document's own claim ("The assumptions carry stated confidence (Table 3)")
then disagreed with every PDF handed in with it.

## Decision

Scan the whole flattened source for the counters first, and **seed** the
body's numbering with the numbered floats that `_strip_frontmatter` removes:

```
full_numbers = _numbering(_blank_latex(flattened))
seeds        = _stripped_float_seeds(flattened, full_numbers)   # title-page floats
numbers      = _numbering(_blank_latex(body), seeds)
```

`_numbering`/`_floats` take an optional `seeds` mapping; labels defined only in
the stripped region are merged into the body's label table, so an `\autoref`
that points at a title-page table still resolves (the Members table's label
prints `Table 1`). The figure pass rescans the post-rewrite text and receives
the same seeds, so figure numbers stay consistent too.

The Markdown's numbers now match the compiled document's lists of figures and
tables (`main.lof`/`main.lot`), and captions and references still come from one
index.

## Consequences

- **+** Numbering is exactly what the compiled PDF numbered: a title-page table
  is Table 1, and body tables continue from it. This is what makes ADR-0058's
  preserved members table explain its own caption.
- **+** The change is mechanical: `seeds` defaults to `None`, so a document
  without stripped floats numbers as before (all existing fixtures unchanged).
- **−** Numbering depends on `_strip_frontmatter`'s removals staying
  float-shaped: the seed counts floats inside the `titlepage` environment
  (the only removed region that can contain them).
- **−** A float whose caption sits outside the stripped region but whose
  environment is inside it (pathological) would be counted twice; the stripper
  removes whole environments, so this cannot happen in practice.

## Alternatives considered

- **Document the offset as intentional** (ADR-0056's outcome). Rejected: the
  mismatch is visible against every compiled artifact and the fix is a few
  lines once the scan runs over the full text anyway.
- **Renumber the Markdown to ignore the title page and rewrite the prose
  references accordingly.** Rejected: it would match the Markdown to itself,
  not to the PDF a reader compares against.
