# 0050. Markdown containing model output is named `-inference-used`

- Status: Accepted
- Date: 2026-10-13

## Context

The optional AI passes (vision transcription, diagram interpretation, paper
structure, Markdown restructure, RAG summary) are the expensive part of a
conversion: they run local models for minutes per document, and the resulting
Markdown is *not* reproducible — re-deriving it costs the same model time again.
Its deterministic counterpart is free to reproduce.

Both kinds of output used to land on the same name (`<stem>.md`, ADR-0048), so
a plain re-run — an emptied AI-flag set, a folder sweep, a GUI batch without the
checkboxes — silently replaced an artifact that cost inference time with one
that cost nothing. Nothing in the file name, and nothing on disk, said which
kind it was.

Constraints:

- The distinction can only be known **after** the conversion: whether a pass
  contributes content depends on the model's reply (word-gate rejections, the
  need gate, unreadable images, a garbled summary that falls back) and on what
  the document contains (a deck with no images produces no vision output even
  with `VISION_ENABLED=1`).
- The name must mean "model output is in this file", not "an AI flag was on":
  a wrongly marked file both lies and wastes the marker, and a wrongly unmarked
  one reintroduces the loss this ADR exists to prevent.
- Naming must never fail, move, or slow a conversion, and must never overwrite
  a marked file by accident.
- The layout rule is "explicit output folder disables all renaming"
  (ADR-0048) — but the protection is about the artifact, not the layout.

## Decision

**A `.md` that received model-produced content is written as
`<stem>-inference-used.md`; an existing marked file is never overwritten.**

1. **Ledger, not flags.** New module `converter/inference.py` keeps a
   per-conversion set of contributing passes in a `contextvars.ContextVar`
   (the same idiom as `logstore`'s run tag), wrapped around each conversion by
   `_convert_one`'s `inference.tracking()`. `note(pass)` is called where model
   output is *accepted into the Markdown*, never where a model call merely
   happens:

   | Pass | Note site | Condition |
   | --- | --- | --- |
   | vision — PPTX image | `pptx._shape_to_md` | `maybe_transcribe_image` returned text |
   | vision — PPTX chart | `pptx._handle_chart` | text survived `transcription_quality` |
   | vision — PDF image | `pdf._page_images` | transcription returned |
   | vision — PDF page/column | `pdf._page_to_md` | `transcribe_columns` returned text |
   | interpret | `pdf._page_to_md` | `interpret_diagram` returned relationships |
   | structure | `pdf.convert` | `structure_paper` returned amended lines |
   | format | `format._reformat_slide` | decision `amended` (word gate passed) |
   | summary | `summary.prepend_summary` | decision `model`, not the deterministic fallback |

   The classifier gate, the pre-flight need gate, and the router are
   decision-only and never note. LaTeX (`ai_passes = {"summary"}`) can only be
   marked by a model-written summary.

2. **Rename at the end of the conversion.** `converter/organize.py` owns the
   marker: `marked_stem()` (never doubles an existing marker), `claim_inference_path()`
   (the marked name, else the next free `-inference-used-2`/`-3`/… with a
   warning), and `mark_inference_output()` (the `os.replace`). `_convert_one`
   calls it after polish + summary, before the archive move, and updates
   `result.md_path`; a failed rename only warns. `ConvertResult.inference_passes`
   reports the contributing passes and a `mark-inference` run phase records them.

3. **Everywhere.** The marker applies in the beside-source layout, with an
   explicit `ptm -o` folder, and to engine/browser uploads — the same content
   rule regardless of where the file lands. The marker appends *after* kebab
   normalization, `duplicate_if_exists` numbering (`deck (2)-inference-used.md`)
   and the 100-char stem truncation, so it is never cut or swallowed.

4. **Never overwrite a marked file.** `claim_inference_path` bumps rather than
   replaces; `avoid_marked_artifact()` additionally keeps a source literally
   named like a marked file (`deck-inference-used.pdf`) from writing its plain
   `<stem>.md` over an existing artifact — it converts to `-2` instead.
   `INFERENCE_OVERWRITE=on` / `ptm --overwrite-inference` / GUI checkbox /
   web checkbox / TUI setting (`overwrite infer`) is the explicit opt-out for a
   deliberate re-run onto the same name.

5. **The archive is untouched.** `source/deck.pdf` keeps its deterministic
   kebab name (it is the source, not the artifact), and `assets/<stem>/` stays
   shared: image bytes are extracted deterministically and deduplicated by
   content hash, so both files reference the same images.

## Consequences

- A string search for `-inference-used` now selects exactly the artifacts whose
  reproduction costs model time; vault tooling can key on it.
- Inference and deterministic outputs of the same source coexist as
  `<stem>.md` and `<stem>-inference-used.md` instead of replacing each other.
- An inference run still *writes* `<stem>.md` first and renames it, so a
  pre-existing **deterministic** `<stem>.md` is replaced by the inference run
  (as a plain re-run would have replaced it — deterministic output is free to
  reproduce). Only inference artifacts gain the new protection.
- Repeated inference runs accumulate `-inference-used`, `-inference-used-2`, …
  unless `--overwrite-inference` is given; the warning names the file that was
  kept.
- Notes that reference the Markdown by name (`[[deck]]`) follow the deterministic
  file after a marked run lands; the marked file must be linked by its full name.
- Detection is only as complete as the note sites: a future pass that emits
  content without calling `inference.note()` would produce unmarked output.
  The ledger's `KNOWN_PASSES` list and this ADR's table are the checklist.

## Alternatives considered

- **Mark whenever an AI flag is enabled** — cheap and known before conversion,
  but the name would lie for every gate-skipped, unreadable, or fell-back
  document, and would not distinguish an actual model contribution.
- **Choose the marked name up front** (for AI-enabled runs) and rename back
  when no content landed — avoids touching the unmarked file, but forces the
  assets folder (`assets/<stem>/`) under the marker name too, breaking the
  shared-assets rule, and leaves file names that change after the fact anyway.
- **Record the marker in the file's front matter** instead of the name — no
  rename, but the guarantee is only visible to tools that parse the file, and
  the common case (Obsidian links, Finder, quick look) does not.
- **Always overwrite the marked file like any re-run** — the user's stated cost
  (an accidental re-run destroying model time) is exactly what this ADR
  prevents; the opt-out flag covers the deliberate case.
- **Marker in the archive copy name too** (`source/deck-inference-used.pdf`) —
  the archive holds the untouched source; marking it would break the ADR-0048
  re-run mapping (a re-run of an archived source produces the same names) for
  no protection gain.
