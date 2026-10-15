# 0058. LaTeX title page preserved as a front block

- Status: Accepted (amends [0056](0056-latex-title-and-crossrefs.md))
- Date: 2026-10-15

## Context

ADR-0056 derived the document title from the title page (the biggest font
tier) and then discarded the rest as "template chrome". Converting the TDT4259
assignment 1 report showed that the discarded content is not chrome:

- the **course and department** (`\textsc{\LARGE Department of Computer
  Science}`, `\textsc{\Large TDT4259 Applied Data Science}`) — context a study
  note needs to be self-contained;
- the **members table** (`\caption{Members}`) — the only place the author name
  and email appear, and the reason the compiled document counts it as Table 1;
- the **word count** (`\IfFileExists{wordcount.tex}{\input{…} words}{N/A}`, a
  flat number `texcount` writes before compile) and the **term**
  (`{\large Autumn 2026}`).

The Markdown lost all of it: `# Assignment 1 - Problem D` was the entire front
block. Pandoc's markdown writer also escaped literal text defensively in
tables (`\~18/yr` for `\textasciitilde{}18/yr`, `\[TBD\]` for `[TBD]`), and the
backslashes render literally in Obsidian.

## Decision

Preserve the title page as a **front block** between the title line and the
body, extracted structurally (no template strings; ADR-0056's title derivation
is unchanged):

- the title page body is scanned after `\IfFileExists{file}{then}{else}` is
  evaluated against the filesystem, so an inlined `\input` (the word count)
  resolves deterministically;
- text separated by `\\` line breaks becomes plain text lines, a run of
  consecutive lines is joined with ` · `, and the line that produced the title
  is dropped;
- `table` floats and bare `tabular` environments become pipe tables; a
  numbered float keeps its caption as a `**Table N: …**` line (the number comes
  from the compiled-document scan, ADR-0059);
- `\includegraphics`, rules, spacing (`\vspace`, `\vfill`, …) and
  `\write18` contribute nothing.

For the NTNU template this yields:

```markdown
# Assignment 1 - Problem D

Department of Computer Science · TDT4259 Applied Data Science

**Table 1: Members**

| Given name | Familyname | Contact |
| --- | --- | --- |
| Snorre | Sæther | snorrks@stud.ntnu.no |

Word count: 2852 words · Autumn 2026
```

A separate post-pandoc pass restores the two literal escapes the writer adds
where they cannot mean anything: `\~` → `~` and `\[…\]` → `[…]`, outside
`$…$`/`$$…$$` math only (a backslash inside math is TeX).

A project that sets `\title`/`\author` keeps exactly the previous title block;
the front block only appears when a `titlepage` environment exists.

## Consequences

- **+** A converted report carries its course, department, term, word count and
  members table; a title-page table's content (and, with ADR-0059, its number)
  is no longer invisible.
- **+** The extraction is structural: any title-page text, table or
  `\IfFileExists` conditional survives, not just the NTNU template's.
- **−** The front block is faithful but flat: small-caps and font sizes are
  not reproduced, and a run of title-page lines reads as one `·`-joined line.
- **−** A `\tabular` with nested content (a `\multicolumn`, a nested tabular)
  is rendered best-effort; ragged rows are padded.
- **−** A title-page `figure` (an image) still contributes nothing beyond the
  title; only text and tables are preserved.

## Alternatives considered

- **Keep dropping the title page** (ADR-0056's decision). Rejected: the report
  is noticeably incomplete without its course, term and author contact.
- **Render the whole `titlepage` environment through pandoc.** Rejected: the
  chrome (`\HRule`, `\vbox`, `\write18`, `\IfFileExists`) emits garbage or
  nothing useful, and the title would be duplicated.
- **Emit YAML front matter instead of visible lines.** Rejected for now: the
  pipeline's conventions are visible Markdown (`# Title` + `*Authors*`), and
  properties would be a second front-block shape to document.
