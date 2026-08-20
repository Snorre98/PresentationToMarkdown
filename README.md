# pptx2md

Desktop app that converts PowerPoint presentations to Markdown. Personal-use tool built for feeding lecture slides into an Obsidian vault.

## Features

- Batch convert multiple `.pptx` files (drag-and-drop or file picker)
- One `.md` per deck, images extracted to `assets/<deck-name>/`
- Slide titles become `#` headings
- Body text keeps **bold**/*italic* run formatting; bullets and numbered lists become Markdown lists
- Tables become pipe tables
- Speaker notes included as blockquotes after each slide

## Install

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

## Run

```bash
./.venv/bin/python main.py
```

Or use the converter library directly from Python:

```python
from converter import convert_files

results = convert_files(["deck1.pptx", "deck2.pptx"], "out/")
for r in results:
    print(r.pptx_path.name, "->", r.md_path or r.error)
```

## Structure

- `converter.py` — core conversion library, no UI dependencies
- `gui.py` — PySide6 interface (file list, output folder, progress, log)
- `main.py` — entry point
- `tests/make_test_deck.py` — generates a synthetic deck covering all features

## Known limitations / future ideas

- Charts and SmartArt are skipped (SmartArt text lives in a separate XML part)
- Markdown flavor toggle (Obsidian `![[wiki-links]]`), note style, and heading level options could go in a settings pane
- Packaging into a standalone executable with PyInstaller
