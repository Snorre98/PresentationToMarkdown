# Presentation to Markdown

Desktop app and reusable Python library that converts PowerPoint presentations (`.pptx`) and PDF documents (`.pdf`) into Markdown. Personal-use tool built for feeding lecture slides into an Obsidian vault.

- **GUI** — drag-and-drop files, batch convert, watch progress and per-file results in a log
- **Library** — importable `converter` package, no UI dependencies

## What it does

One Markdown file per source document, with images extracted to a sidecar `assets/<name>/` folder. Both formats preserve **bold** and *italic* formatting.

### PowerPoint (`.pptx`)

| Source element | Markdown output |
| --- | --- |
| Slide title | `# Title` heading (falls back to `# Slide N` when absent) |
| Text runs | `**bold**`, `*italic*`, `***both***` preserved |
| Bullets | `- ` list items |
| Numbered lists | `1. ` list items |
| Nested levels | Indented with two spaces per level |
| Tables | Pipe tables (`\|` and newlines escaped) |
| Images | Extracted to `assets/<name>/` and referenced |
| Speaker notes | `> **Notes:**` blockquote after each slide |

Also: slide-number and date placeholders are skipped, headers/footers are italicized, groups are walked recursively, and charts are skipped with a warning.

### PDF (`.pdf`)

| Source element | Markdown output |
| --- | --- |
| Each page | `# Page N` heading |
| Text | Extracted block-by-block, preserving bold/italic via font flags |
| Images | Extracted to `assets/<name>/` and referenced |

## How to use

### GUI

```bash
./.venv/bin/python main.py
```

1. **Add files** — click *Add Files...* to pick `.pptx`/`.pdf` files, *Add Folder...* to scan a folder (and subfolders) for supported files, or drag-and-drop either onto the list.
2. **Adjust the list** — *Remove* selected entries or *Clear* everything.
3. **Pick an output folder (optional)** — by default each file is written to its own `<input-folder>/markdown/`; enter a path and click *Browse...* to override and send everything to one folder instead.
4. **Convert** — click *Convert*. Conversion runs on a background thread, so the UI stays responsive while a progress bar advances and the log reports one line per file:
   - `[OK]  deck.pptx -> /path/deck.md`
   - `[ERR] broken.pdf: ...` on failure
   - `[WARN] ...` for non-fatal issues (e.g. skipped charts)

### Library

```python
from converter import convert_file, convert_files, SUPPORTED_EXTENSIONS

# one file (output defaults to <input-folder>/markdown)
result = convert_file("deck.pptx")
print(result.md_path or result.error)

# one file, explicit output folder
result = convert_file("deck.pptx", "out/")

# many files, with progress
def on_progress(idx, total, name):
    print(f"[{idx}/{total}] {name}")

results = convert_files(["deck1.pptx", "handout.pdf"], "out/", on_progress)
for r in results:
    print(r.source_path.name, "->", r.md_path or r.error)
    for warning in r.warnings:
        print("  warn:", warning)
```

- `convert_file(path, output_dir=None) -> ConvertResult`
- `convert_files(paths, output_dir=None, progress_callback=None) -> list[ConvertResult]`
- `progress_callback(idx: int, total: int, name: str) -> None`
- `ConvertResult` fields: `source_path`, `md_path`, `error`, `warnings`
- `SUPPORTED_EXTENSIONS` — e.g. `{".pptx", ".pdf"}`
- `output_dir` is optional; when omitted, each file is written to `<source-folder>/markdown/`.

## Output layout

```text
out/
├── deck.md
├── handout.md
└── assets/
    ├── deck/
    │   └── deck_01_19b314cf.png
    └── handout/
        └── handout_01_164ea19d.png
```

Images are deduplicated by content hash and named `<name>_<NN>_<hash>.<ext>`.

With the default `output_dir=None`, the same layout is written under each source file's own folder, i.e. `<input-folder>/markdown/`.

## Example

A slide with title "Bullets and *formatting*", a couple of bullets, and a note becomes:

```markdown
# Bullets and \*formatting\*

- Plain point
- **Bold ***italic* tail
  - Nested child

> **Notes:**
> First line
> Second line.
```

## Install

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

## Structure

- `converter/` — conversion library, no UI dependencies
  - `__init__.py` — public API and extension-based dispatch (`convert_file`, `convert_files`)
  - `base.py` — shared `Converter` interface, registry, and reusable Markdown helpers
  - `pptx.py` — PowerPoint converter (python-pptx)
  - `pdf.py` — PDF converter (PyMuPDF)
- `gui.py` — PySide6 interface (file list, output folder, progress, log)
- `main.py` — entry point
- `tests/make_test_deck.py` — generates a synthetic deck covering all features
- `tests/make_test_pdf.py` — generates a synthetic PDF for testing
- `tests/test_converters.py` — pytest smoke tests for both converters

## Adding a new file format

1. Create `converter/<format>.py` and subclass `Converter`:
   ```python
   from converter.base import Converter, ConvertResult

   class DocxConverter(Converter):
       extensions = (".docx",)

       def convert(self, path, output_dir) -> ConvertResult:
           ...
   ```
2. Register it in `converter/__init__.py`:
   ```python
   registry.register(DocxConverter)
   ```
3. Add the extension to the GUI file filter and placeholder text if you want it listed.

## Known limitations / future ideas

- Charts and SmartArt are skipped (SmartArt text lives in a separate XML part)
- PDF text is extracted block-by-block; no table detection, and multi-column layouts may read out of order
- PyMuPDF is AGPL-3.0 (or commercial) licensed — fine for personal use, review if you distribute
- Markdown flavor toggle (Obsidian `![[wiki-links]]`), note style, and heading level options could go in a settings pane
- Packaging into a standalone executable with PyInstaller
