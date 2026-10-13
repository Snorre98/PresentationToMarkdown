# 0048. Markdown beside the source; sources archived to `source/` under kebab-case names

- Status: Accepted
- Date: 2026-10-13

## Context

The default output layout has always been `<source-folder>/markdown/<stem>.md`
(ADR-0003, ADR-0028): converted Markdown accumulates in a `markdown/` subfolder
while the `.pdf`/`.pptx` sources stay put. In the vault workflow this puts the
generated notes one level down from where the sources live and keeps mixing
converted and unconverted files in the same folder; re-running a folder sweep
after a new download is the only way to tell them apart.

The desired end state is the **opposite**: the Markdown is the primary artifact
in the folder and stays where the source was, while the source file is archived
out of the way — with both names normalized to one stable, predictable
kebab-case form so re-downloads and re-runs map to the same paths.

Constraints:

- Existing explicit-output behaviour (`ptm -o DIR`, the GUI/Web output field) is
  a deliberate escape hatch used for A/B comparisons (`cli_ab`) and must keep
  writing Markdown under its original name with the sources untouched.
- The engine's upload flow (ADR-0027/0028) converts a staged copy but must name,
  move, and compress the **original** on disk.
- LaTeX is a project format: its unit is a folder, not a single file.

## Decision

**Default conversions write the `.md` beside the source and move the converted
source into a sibling `source/` folder, both named by a kebab-case normalization
of the source.** An explicit output folder disables the organizing entirely.

1. **Layout** (`converter/organize.py`). With no explicit `output_dir`:

   ```text
   lectures/03 What makes things fun.pdf
     -> lectures/03-what-makes-things-fun.md
     -> lectures/source/03-what-makes-things-fun.pdf
   ```

   `default_output_dir(path)` is the source's own folder, or the archive
   folder's parent for a file already inside `source/`. The move happens only
   after a successful conversion; a failed conversion leaves the source in
   place. With `IMAGE_LINKS=on` the assets sidecar sits next to the Markdown
   (`assets/<kebab-stem>/`), so its relative links stay valid.

2. **Kebab normalization** (`kebab_stem`). Lowercase; accents folded to ASCII
   (NFKD + combining-mark strip, plus `ø→o, æ→ae, å→a, ß→ss, đ/ð→d, þ→th,
   ł→l, œ→oe`); every run of non-alphanumerics becomes a single `-`; leading
   digits are kept; non-Latin letters are preserved; names are capped at 100
   characters (cut at a hyphen boundary) and an unusable name becomes
   `document`. The archived source gets the same stem with its original
   (lowercased) extension.

3. **Already-archived files** (directly inside a `source/` folder) are not moved
   again: their Markdown is (re)written in the folder's parent, so a full
   re-scan and a re-conversion are idempotent. Recursive folder scans
   (`collect_inputs`, used by CLI/GUI/engine) **skip `source/` directories**;
   a file or `source/` folder can still be converted explicitly.

4. **Collisions.** Two different sources whose names normalize identically in
   one batch get a `-2`, `-3`, … suffix on both the Markdown and the archive
   copy, plus a warning — nothing is silently overwritten. Across runs the
   archive copy is **replaced** (deterministic names = move semantics), using
   an existing-copy-moved-aside dance that restores the previous archive if the
   move fails. A folder literally named `source` cannot archive into itself and
   is left in place with a warning.

5. **Explicit output** (`str | Path`) disables rename, move, and compression.
   A **callable** output resolver (ADR-0028) also disables organizing unless a
   `source_resolver` is supplied, in which case the resolver names the on-disk
   file a conversion represents — the engine maps each staged upload to its
   original, so the original is renamed/moved/compressed while the Markdown goes
   beside it. `convert_file`/`convert_files` gain `organize: bool | None` and
   `source_resolver`; `ConvertResult` gains `moved_to`. Recent-files recording
   prefers `moved_to`, so a re-opened recent entry points at the archived file.

6. **LaTeX.** A project folder moves as a unit (`thesis/` → `source/thesis/`)
   with `thesis.md` beside it (the project's parent, as today's layout already
   did for the output). A lone `.tex` file moves as a file; multi-file projects
   should be converted by pointing at the project folder.

## Consequences

- The folder's primary artifact is now the `.md`; sources no longer sit next to
  the notes. Existing `markdown/` folders are left untouched (no migration).
- `duplicate_if_exists` (ADR-0015) applies to the kebab stem (the Finder
  ` (2)` suffix, e.g. `deck (2).md`); the prior output and its assets folder
  are still never touched.
- Uploads' originals are organized on the real filesystem (the engine is the
  writer); a staging upload with no known original keeps the prune-safe
  `uploads/markdown/` fallback and is not moved.
- The RAG document index keys on the pre-move source path, so a later
  re-conversion from `source/` creates a new document row (content-hash dedup
  limits the cost). Accepted.
- The web/TUI "default output" hints and open-folder targets now point at the
  source's folder rather than a `markdown/` subfolder.

## Alternatives considered

- **Keep `<source>/markdown/` and only archive sources** — the request is the
  opposite ordering: the Markdown belongs where the source was. Rejected.
- **Move the source but keep its original name** — names with spaces, case
  variants, and accents break cross-platform links and make re-downloads
  ambiguous; normalizing both sides is what makes re-runs deterministic.
- **Auto-suffix on every archive collision** — never loses data, but repeated
  re-downloads pile up `-2`, `-3` copies of the same lecture. Rejected in favour
  of deterministic replacement, with in-batch normalization collisions still
  suffixed so distinct sources cannot overwrite each other.
- **Archive in place (rename next to the file)** — leaves two files side by side
  and doesn't get the sources out of the way. Rejected.
- **Copy instead of move** — doubles disk usage for large decks/PDFs and keeps
  the ambiguity between converted and unconverted files. Rejected.
- **A toggle for the whole layout** — the vault workflow wants this always; the
  explicit-output escape hatch covers the A/B and "collect everything here"
  cases without a second preference.
