"""LaTeX project -> Markdown converter built on pandoc (ADR-0038).

A LaTeX *project* is a directory containing ``.tex`` source (plus an optional
``.bib`` and image assets). The converter flattens ``\\input``/``\\include``/
``\\import``/``\\subimport`` into a single source, hands it to pandoc, then
post-normalizes the output into the pipeline's conventions:

- a synthetic ``# Title`` + ``*Authors*`` block (``\\title``/``\\author``, else
  the title page's biggest font tier, else the directory name; ADR-0056),
- pandoc ``--shift-heading-level-by=1`` so ``\\section`` becomes ``##`` (keeping
  the top-level ``#`` chunking contract for ``format``/``summary``),
- pandoc's ``{#…}`` attributes stripped and ``\\autoref``/``\\ref``/``\\eqref``
  cross-references turned into plain text (``Section 3``, ``Figure 1``,
  ``(4)``) from a deterministic numbering pass over the source (ADR-0056),
- figures become a numbered ``**Figure N: caption**`` line, plus an image with
  ``IMAGE_LINKS=on``; TikZ/pgfplots figures are rendered through ``standalone``
  + pdflatex (ADR-0057). Captioned tables become ``**Table N: caption**``.
- with ``IMAGE_LINKS=on`` (legacy) image references are rewritten into
  ``assets/<stem>/`` with content-hash deduplication (``base.write_image``) and
  display math is rendered to a PNG via ``standalone`` + pdflatex + PyMuPDF.
  By default (ADR-0047) no image is written — captions stand in for figures —
  and display math stays as the ``$$...$$`` passthrough. Inline ``$...$`` math
  is always left as passthrough for Obsidian.

pandoc is a *required* external binary for ``.tex`` input; pdflatex is required
only for display-math and TikZ-figure rendering (``IMAGE_LINKS=on``), and both
degrade with a warning. Two lazy environment preferences tune the figure pass
(ADR-0057): ``LATEX_FIGURES=off`` disables it and ``LATEX_FIGURE_PREAMBLE``
names a ``.tex`` file appended to the render preamble.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf as fitz

from converter import config
from converter.base import (
    ConvertResult,
    Converter,
    PageProgressCallback,
    _link_dest,
    write_image,
)

PANDOC_PATH = os.environ.get("PANDOC_PATH", "pandoc")
PDLATEX_PATH = os.environ.get("PDLATEX_PATH", "pdflatex")

_IMPORT_RE = re.compile(
    r"\\(?P<cmd>subimport|import|input|include)"
    r"\s*\{(?P<first>[^{}]*)\}"
    r"(?:\s*\{(?P<second>[^{}]*)\})?"
)

_TITLEPAGE_RE = re.compile(
    r"\\begin\{titlepage\}(?P<body>.*?)\\end\{titlepage\}", re.DOTALL
)

_IMAGE_RE = re.compile(r"!\[(?P<alt>.*?)\]\((?P<url>[^)]*)\)", re.DOTALL)

_DISPLAY_MATH_RE = re.compile(r"\$\$(?P<body>.*?)\$\$", re.DOTALL)

_BIB_CMD_RE = re.compile(r"\\(?:addbibresource|bibliography)\s*\{([^{}]+)\}")

_ATTR_RE = re.compile(r"(\]\([^)]*\))\{[^{}]*\}")

# A pandoc attribute at the end of a line: headings, table caption lines.
_LINE_ATTR_RE = re.compile(r"[ \t]*\{#[^}\s]*\}[ \t]*$", re.MULTILINE)

# A pandoc span attribute after inline text: ``[scikit-learn developers]{.nocase}``.
_SPAN_ATTR_RE = re.compile(r"\[(?P<text>[^\[\]\n]+)\]\{[.#][^{}\s]*\}")

# A numbered table caption line, as emitted by pandoc after a pipe table.
_MD_TABLE_CAPTION_RE = re.compile(
    r"^[ \t]*:[ \t]+(?P<caption>Table \d+:[^\n]*?)[ \t]*$", re.MULTILINE
)

_CIT_ENTRY_RE = re.compile(
    r"^:{3,}\s*\{[^{}]*\.csl-entry[^{}]*\}\s*\n(.*?)\n:{3,}\s*$",
    re.DOTALL | re.MULTILINE,
)

_BIB_BODY_RE = re.compile(
    r"^:{3,}\s*\{[^{}]*\.csl-bib-body[^{}]*\}\s*$",
    re.MULTILINE,
)

_FENCE_RE = re.compile(r"^:{3,}\s*$", re.MULTILINE)

# --- Title-page derivation (ADR-0056) -------------------------------------
# Font tiers, highest first: the biggest line on the title page is its title.
# ``\textsc`` lines lose ties (course/department names are small-caps).
_FONT_TIERS: tuple[str, ...] = ("Huge", "huge", "LARGE", "Large", "large")

# Environments whose body must never be scanned or rewritten (their content is
# code, not LaTeX) — blanked out before numbering/ref rewriting.
_VERBATIM_ENVS: tuple[str, ...] = (
    "verbatim",
    "verbatim*",
    "lstlisting",
    "lstlisting*",
    "minted",
    "comment",
)

# --- Cross-reference rewriting (ADR-0056) ---------------------------------
# Label prefix -> the word ``\autoref`` prints in front of the number. A label
# whose prefix is absent here keeps pandoc's raw ``[label](#label)`` anchor.
_REF_WORDS: dict[str, str] = {
    "sec": "Section",
    "section": "Section",
    "subsec": "Section",
    "chap": "Chapter",
    "chapter": "Chapter",
    "fig": "Figure",
    "figure": "Figure",
    "tab": "Table",
    "table": "Table",
    "eq": "Equation",
    "equation": "Equation",
    "eqn": "Equation",
    "app": "Appendix",
    "appendix": "Appendix",
    "alg": "Algorithm",
    "algorithm": "Algorithm",
    "lst": "Listing",
    "listing": "Listing",
}

_REF_CMD_RE = re.compile(r"\\(?P<cmd>autoref|ref|eqref|cref|Cref)\s*\{(?P<label>[^{}\s]+)\}")

# A pandoc-resolved cross-reference link left in the Markdown: ``[3](#sec:x)``.
_MD_REF_RE = re.compile(
    r"\[(?P<text>[^\]\n]*)\]\(#(?P<label>[^)\s]+)\)(?:\{(?P<attrs>[^{}]*)\})?"
)

# --- Figure and table captions (ADR-0057) ---------------------------------
_CMD_RE = re.compile(
    r"\\(?P<cmd>begin|end|caption|label|chapter|section|subsection|subsubsection)"
    r"(?P<star>\*?)"
)

_FLOAT_ENV_RE = re.compile(
    r"\\begin\{(?P<env>figure|table)\*?\}"
    r"(?P<body>.*?)"
    r"\\end\{(?P=env)\*?\}",
    re.DOTALL,
)

_CAPTION_SKIP_RE = re.compile(
    r"\\setlength\{\w*captionskip\}\{[^{}]*\}|"
    r"\\vspace\*?\{[^{}]*\}|"
    r"\\vskip\s*[-\d.]+(?:pt|em|ex|mm|cm)\b|"
    r"\\hspace\*?\{[^{}]*\}"
)

_NUMBERED_EQ_ENVS: frozenset[str] = frozenset(
    {"equation", "align", "gather", "multline", "eqnarray", "alignat"}
)

_SECTION_LEVELS: dict[str, int] = {
    "chapter": 0,
    "section": 1,
    "subsection": 2,
    "subsubsection": 3,
}

_SECTION_PREFIXES: frozenset[str] = frozenset(
    {"sec", "section", "subsec", "subsection", "subsubsec", "subsubsection"}
)

_CAPTION_CMD_RE = re.compile(r"\\caption(?P<star>\*?)")
_LABEL_CMD_RE = re.compile(r"\\label")
_INCLUDEGRAPHICS_RE = re.compile(r"\\includegraphics\*?")

# The standalone preamble a TikZ/pgfplots figure is rendered with (ADR-0057).
# The project's own style declarations are appended after it, and
# ``LATEX_FIGURE_PREAMBLE`` (a .tex file) last.
_FIGURE_PREAMBLE_DEFAULT = r"""\documentclass[preview,border=4pt]{standalone}
\usepackage[T1]{fontenc}
\usepackage{amsmath,amssymb,bm}
\usepackage{textcomp}
\usepackage{xcolor}
\usepackage{graphicx}
\usepackage{tikz}
\usetikzlibrary{shapes.geometric, arrows.meta, positioning, calc, fit, patterns, decorations.pathreplacing, decorations.markings, matrix, backgrounds}
\usepackage{pgfplots}
\pgfplotsset{compat=1.18}
"""

# How many braced arguments a collected style command takes (default 1).
_STYLE_ARG_COUNT: dict[str, int] = {"definecolor": 3, "colorlet": 2}

# A figure marker survives pandoc as a plain image: ``![](ptmfig-2)``.
_FIGURE_MARKER = "ptmfig-{n}"
_FIGURE_MARKER_RE = re.compile(r"!\[[^\]]*\]\((?P<marker>ptmfig-\d+)\)")

_TIKZ_RE = re.compile(r"\\begin\{(?:tikzpicture|[a-z]*axis|pgfplots)\}")

_STYLE_CMD_RE = re.compile(r"\\(?:tikzset|tikzstyle|pgfplotsset|colorlet|definecolor|usetikzlibrary)\b")


def pandoc_available() -> bool:
    if os.path.isabs(PANDOC_PATH):
        return Path(PANDOC_PATH).exists()
    return shutil.which(PANDOC_PATH) is not None


def latex_available() -> bool:
    if os.path.isabs(PDLATEX_PATH):
        return Path(PDLATEX_PATH).exists()
    return shutil.which(PDLATEX_PATH) is not None


def _uncomment(line: str) -> str:
    """Return ``line`` up to the first unescaped ``%`` (a LaTeX comment)."""
    i = 0
    while i < len(line):
        if line[i] == "\\" and i + 1 < len(line) and line[i + 1] == "%":
            i += 2
            continue
        if line[i] == "%":
            return line[:i]
        i += 1
    return line


def _flatten(
    text: str,
    base_dir: Path,
    stack: set[Path],
    depth: int,
    warnings: list[str],
) -> str:
    """Inline ``\\input``/``\\include``/``\\import``/``\\subimport`` content.

    Resolves paths relative to the importing file's directory, appends ``.tex``
    when no extension is given, and skips circular or missing includes with a
    warning. Commented-out directives (after an unescaped ``%``) are ignored.
    """
    if depth > 20:
        warnings.append("Include nesting too deep; stopping flattening")
        return text
    out: list[str] = []
    for line in text.split("\n"):
        code = _uncomment(line)
        m = _IMPORT_RE.search(code)
        if not m:
            out.append(line)
            continue
        cmd = m.group("cmd")
        first = m.group("first").strip()
        second = (m.group("second") or "").strip()
        if cmd in ("import", "subimport") and second:
            rel = f"{first}/{second}"
        else:
            rel = first
        if not rel:
            out.append(line)
            continue
        if not Path(rel).suffix:
            rel += ".tex"
        target = (base_dir / rel).resolve()
        if not target.exists():
            warnings.append(f"Could not resolve \\{cmd}{{{first}}}{{{second or ''}}}: {target}")
            out.append(line)
            continue
        if target in stack:
            warnings.append(f"Skipping circular \\{cmd}: {target}")
            out.append(line)
            continue
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            warnings.append(f"Could not read {target}: {exc}")
            out.append(line)
            continue
        stack.add(target)
        try:
            replacement = _flatten(content, target.parent, stack, depth + 1, warnings)
        finally:
            stack.discard(target)
        out.append(line[: m.start()] + replacement + line[m.end() :])
    return "\n".join(out)


def _braced(source: str, cmd: str) -> str | None:
    """Return the balanced ``{...}`` argument of ``\\cmd``, or ``None``."""
    m = re.search(r"\\" + re.escape(cmd) + r"\*?\s*\{", source)
    if not m:
        return None
    i = m.end()
    depth = 1
    while i < len(source) and depth > 0:
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
        i += 1
    return source[m.end() : i - 1]


def _strip_latex(text: str) -> str:
    """Best-effort conversion of a short LaTeX string to plain text."""
    text = re.sub(r"(?<!\\)%.*", "", text)
    text = text.replace("\\\\", " · ")
    text = text.replace("\\and", " · ")
    text = re.sub(r"\\[a-zA-Z]+\*?\s*\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\[a-zA-Z]+", "", text)
    text = re.sub(r"[{}]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _strip_frontmatter(source: str) -> str:
    """Clean up the flattened source before pandoc.

    Drops the title page, TOC and list commands (pandoc emits none usefully),
    strips ``\\includegraphics`` optional ``[width=...]`` arguments and
    ``\\begin{figure}[htbp]``/``\\begin{table}[htbp]`` placement options so
    pandoc emits plain Markdown images/tables rather than raw-HTML blocks.
    """
    source = _TITLEPAGE_RE.sub("", source)
    for cmd in ("tableofcontents", "listoffigures", "listoftables", "maketitle"):
        source = re.sub(r"\\" + cmd + r"\b[^\n]*", "", source)
    source = re.sub(r"\\includegraphics\s*\[[^\]]*\]", r"\\includegraphics", source)
    source = re.sub(
        r"\\begin\{((?:figure|table)\*?)\}\s*\[[^\]]*\]",
        r"\\begin{\1}",
        source,
    )
    return source


# ---------------------------------------------------------------------------
# Deterministic source analysis: masked scanning, numbering, cross-references
# (ADR-0056), figure planning (ADR-0057)
# ---------------------------------------------------------------------------


def _balanced_end(text: str, open_index: int) -> int:
    """Index just past the ``}`` matching the ``{`` at ``open_index``."""
    depth = 0
    i = open_index
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(text)


def _arg_bounds(text: str, index: int) -> tuple[int, int] | None:
    """Span of the balanced ``{…}`` argument at/after ``index`` (or ``None``)."""
    i = index
    while i < len(text) and text[i] in " \t\n\r":
        i += 1
    if i >= len(text) or text[i] != "{":
        return None
    return i, _balanced_end(text, i)


def _arg_at(text: str, index: int) -> str | None:
    """Content of the balanced ``{…}`` argument at/after ``index``."""
    bounds = _arg_bounds(text, index)
    if bounds is None:
        return None
    return text[bounds[0] + 1 : bounds[1] - 1]


def _brace_content(line: str) -> str | None:
    """Content of the first balanced ``{…}`` group in ``line``, if any."""
    start = line.find("{")
    if start < 0:
        return None
    end = _balanced_end(line, start)
    return line[start + 1 : end - 1]


def _blank_latex(text: str) -> str:
    """Mask comments and verbatim-like environments, keeping offsets stable.

    Numbering and cross-reference rewriting run over this copy, so a ``\\ref``
    inside a code listing (``lstlisting``, ``minted``, ``verbatim``) or on a
    commented-out line is never rewritten. Characters become spaces and line
    breaks are kept, so match spans still address the original text.
    """

    def blank(match: re.Match[str]) -> str:
        return "".join("\n" if c == "\n" else " " for c in match.group(0))

    masked = text
    for env in _VERBATIM_ENVS:
        name = re.escape(env)
        masked = re.sub(
            r"\\begin\{" + name + r"\}.*?\\end\{" + name + r"\}",
            blank,
            masked,
            flags=re.DOTALL,
        )
    lines: list[str] = []
    for line in masked.split("\n"):
        code = _uncomment(line)
        lines.append(code + " " * (len(line) - len(code)))
    return "\n".join(lines)


def _font_tier(line: str) -> int:
    """Highest font-size tier present in ``line`` (0 = none; ``\\large`` = 1)."""
    for rank, name in enumerate(reversed(_FONT_TIERS), start=1):
        if f"\\{name}" in line or f"\\begin{{{name}}}" in line:
            return rank
    return 0


def _titlepage_body(source: str) -> str | None:
    """The ``titlepage`` environment's content, or ``None`` when absent."""
    match = _TITLEPAGE_RE.search(source)
    if match is None:
        return None
    return match.group("body")


def _title_from_titlepage(body: str | None) -> str:
    """Derive a title from the title page (ADR-0056).

    The biggest font tier wins (``{\\huge \\bfseries Assignment 1 - Problem D}``
    in the NTNU template); ``\\textsc`` lines lose ties because they carry the
    course/department name. Fallback: the title page's first bold line. Returns
    ``""`` when nothing is recognizable, so the caller keeps the folder name.
    """
    if not body:
        return ""
    ranked: list[tuple[int, int, str]] = []
    for raw in body.split("\n"):
        line = _uncomment(raw)
        tier = _font_tier(line)
        if not tier:
            continue
        text = _strip_latex(_brace_content(line) or line)
        if text:
            ranked.append((tier, 1 if "\\textsc" in line else 0, text))
    if ranked:
        # Highest tier first, non-``\textsc`` first, then document order.
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return ranked[0][2]
    for raw in body.split("\n"):
        line = _uncomment(raw)
        if "\\bfseries" in line or "\\textbf" in line:
            text = _strip_latex(_brace_content(line) or line)
            if text:
                return text
    return ""


_IF_FILE_EXISTS_RE = re.compile(r"\\IfFileExists\s*\{")


def _expand_if_file_exists(text: str, base_dir: Path) -> str:
    """Resolve ``\\IfFileExists{file}{then}{else}`` against the filesystem.

    The standard kernel conditional survives flattening (an existing file's
    ``\\input`` is inlined into its *then* branch), so evaluating it here keeps
    a title page's conditional text — the NTNU template's word count — deterministic.
    """
    out: list[str] = []
    index = 0
    while True:
        match = _IF_FILE_EXISTS_RE.search(text, index)
        if match is None:
            out.append(text[index:])
            break
        out.append(text[index : match.start()])
        pos = match.end() - 1
        args: list[str] = []
        for _ in range(3):
            bounds = _arg_bounds(text, pos)
            if bounds is None:
                break
            args.append(text[bounds[0] + 1 : bounds[1] - 1])
            pos = bounds[1]
        if len(args) != 3:
            out.append(text[match.start() : pos])
        else:
            name, then, otherwise = args
            try:
                exists = (base_dir / name).is_file()
            except OSError:
                exists = False
            out.append(_expand_if_file_exists(then if exists else otherwise, base_dir))
        index = pos
    return "".join(out)


# Layout-only commands a title-page text line never contributes content from.
# Arguments may not cross a line or nest, so the match never swallows the text
# line that follows the command.
_FRONT_DROP_RE = re.compile(
    r"\\(?:vspace|hspace|vskip|setlength|addtolength|rule|vbox|hbox|makebox|"
    r"framebox|parbox|resizebox|scalebox|fontsize|selectfont|write18|immediate|"
    r"newpage|clearpage|pagebreak|linebreak|newline|smallskip|medskip|bigskip|"
    r"noindent|centering|raggedright|raggedleft|vfill|hfill|quad|qquad|par)\*?"
    r"(?:[ \t]*(?:\[[^\]\n]*\]|\{[^{}\n]*\}))*",
    re.IGNORECASE,
)


def _front_text(text: str, base_dir: Path) -> str:
    """Plain text of a title-page line or caption (chrome commands dropped).

    ``\\textsc{\\LARGE Department …}`` becomes ``Department …`` and
    ``\\includegraphics``/rules/spacing disappear, while inline ``$…$`` math is
    kept intact. Used for the front block of :func:`_front_items`.
    """
    text = "\n".join(_uncomment(line) for line in text.split("\n"))
    text = _expand_if_file_exists(text, base_dir)
    text = re.sub(r"\\(?:begin|end)\s*\{[^{}]*\}", " ", text)
    text = re.sub(
        r"\\includegraphics\s*(?:\[[^\]]*\]\s*)?\{[^{}]*\}", " ", text
    )
    text = _FRONT_DROP_RE.sub(" ", text)
    text = re.sub(r"\\\\\s*(?:\[[^\]]*\])?", " ", text)
    for _ in range(4):
        unwrapped = re.sub(r"\\[a-zA-Z@]+\*?\s*\{([^{}]*)\}", r" \1 ", text)
        if unwrapped == text:
            break
        text = unwrapped
    text = re.sub(r"\\[a-zA-Z@]+\*?", " ", text)
    text = text.replace("~", " ")
    text = re.sub(r"[{}]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _tabular_rows(latex: str, base_dir: Path) -> list[list[str]]:
    """Rows of the first ``tabular`` environment in ``latex``, as plain cells.

    The column specification is skipped, ``&`` splits cells, ``\\\\`` splits
    rows and the booktabs/``\\hline`` rules disappear. Ragged rows are
    tolerated by the caller, which pads them.
    """
    match = re.search(r"\\begin\{tabular\*?\}", latex)
    if match is None:
        return []
    start = match.end()
    spec = _arg_bounds(latex, start)
    if spec is not None:
        start = spec[1]
    end = latex.find(r"\end{tabular}", start)
    if end < 0:
        end = len(latex)
    body = re.sub(r"\\(?:hline|toprule|midrule|bottomrule)\b", " ", latex[start:end])
    body = re.sub(r"\\cline\s*\{[^{}]*\}", " ", body)
    rows: list[list[str]] = []
    for raw in re.split(r"\\\\", body):
        raw = raw.strip()
        if not raw:
            continue
        cells = [_front_text(cell, base_dir) for cell in raw.split("&")]
        rows.append([cell.replace("|", r"\|") for cell in cells])
    return rows


@dataclass(frozen=True)
class _FrontTable:
    """A title-page ``table`` float preserved in the front block (ADR-0058)."""

    number: int | None
    caption: str
    rows: list[list[str]]


_TABULAR_ENV_RE = re.compile(
    r"\\begin\{tabular\*?\}.*?\\end\{tabular\*?\}", re.DOTALL
)


def _front_items(
    flattened: str,
    numbers: _Numbering,
    base_dir: Path,
    title: str,
) -> list[str | _FrontTable]:
    """The title page's content, in source order (ADR-0058).

    Text lines become plain paragraphs (a run of consecutive lines joined with
    ``·``, the title line itself dropped); ``table``/``tabular`` floats become
    :class:`_FrontTable` items numbered like the compiled document. A LaTeX
    line break (``\\\\``, its ``[…]`` spacing dropped) ends a line — a soft
    source newline does not — so an inlined ``\\input`` never splits a line.
    Chrome (logo, rules, spacing, ``\\write18``) contributes nothing.
    """
    match = _TITLEPAGE_RE.search(flattened)
    if match is None:
        return []
    body = _expand_if_file_exists(match.group("body"), base_dir)
    blanked = _blank_latex(body)
    table_floats = [
        float_
        for float_ in numbers.floats
        if float_.kind == "table" and match.start() <= float_.start < match.end()
    ]
    regions: list[tuple[int, int, _Float | None]] = [
        (env.start(), env.end(), float_)
        for env, float_ in zip(
            (
                env
                for env in _FLOAT_ENV_RE.finditer(blanked)
                if env.group("env") == "table"
            ),
            table_floats,
        )
    ]
    # A bare ``tabular`` (no ``table`` float) carries no number but its rows
    # still belong in the front block.
    for tabular in _TABULAR_ENV_RE.finditer(blanked):
        if not any(start <= tabular.start() < end for start, end, _ in regions):
            regions.append((tabular.start(), tabular.end(), None))
    regions.sort(key=lambda region: region[0])
    items: list[str | _FrontTable] = []
    run: list[str] = []

    def flush() -> None:
        if run:
            items.append(" · ".join(run))
            run.clear()

    def scan(segment: str) -> None:
        for chunk in re.split(r"\\\\\s*(?:\[[^\]]*\])?", segment):
            text = _front_text(chunk, base_dir)
            if text and text != title:
                run.append(text)

    def table_item(start: int, end: int, float_: _Float | None) -> _FrontTable:
        caption = ""
        caption_cmd = _CAPTION_CMD_RE.search(blanked, start, end)
        if caption_cmd is not None:
            bounds = _arg_bounds(blanked, caption_cmd.end())
            if bounds is not None:
                caption = _front_text(body[bounds[0] + 1 : bounds[1] - 1], base_dir)
        return _FrontTable(
            float_.number if float_ is not None else None,
            caption,
            _tabular_rows(body[start:end], base_dir),
        )

    cursor = 0
    for start, end, float_ in regions:
        scan(body[cursor:start])
        flush()
        items.append(table_item(start, end, float_))
        cursor = end
    scan(body[cursor:])
    flush()
    return items


def _front_markdown(items: list[str | _FrontTable]) -> str:
    """Render :func:`_front_items` as the Markdown front block (ADR-0058).

    Text items become paragraphs; a table item becomes its numbered caption
    line plus a pipe table (the first tabular row is the header).
    """
    blocks: list[str] = []
    for item in items:
        if isinstance(item, str):
            blocks.append(item)
            continue
        if not item.rows:
            continue
        width = max(len(row) for row in item.rows)
        grid = [row + [""] * (width - len(row)) for row in item.rows]
        lines: list[str] = []
        if item.number is not None and item.caption:
            lines.extend([f"**Table {item.number}: {item.caption}**", ""])
        lines.append("| " + " | ".join(grid[0]) + " |")
        lines.append("| " + " | ".join("---" for _ in range(width)) + " |")
        for row in grid[1:]:
            lines.append("| " + " | ".join(row) + " |")
        blocks.append("\n".join(lines))
    return _unescape_literals("\n\n".join(blocks))


def _stripped_float_seeds(flattened: str, numbers: _Numbering) -> dict[str, int]:
    """Float counters consumed before the body begins (ADR-0059).

    ``_strip_frontmatter`` removes the title page, but the compiled document
    still counts the floats it held (the NTNU template's members table is
    Table 1 in ``main.lot``). The seed continues the body scan from there, so
    the Markdown's numbers match the compiled document's.
    """
    spans = [match.span() for match in _TITLEPAGE_RE.finditer(flattened)]
    seeds = {"figure": 0, "table": 0}
    for float_ in numbers.floats:
        if float_.number is None:
            continue
        if any(start <= float_.start < end for start, end in spans):
            seeds[float_.kind] += 1
    return seeds


@dataclass(frozen=True)
class _Caption:
    """A numbered float caption found in the source."""

    kind: str  # "figure" | "table"
    number: int
    arg_start: int  # first character of the caption text
    arg_end: int  # just past the caption text


@dataclass(frozen=True)
class _Float:
    """A ``figure``/``table`` environment with its caption and number."""

    kind: str  # "figure" | "table"
    start: int  # index of ``\begin{…}``
    end: int  # just past ``\end{…}``
    inner_start: int  # body start (after the placement option)
    inner_end: int  # body end
    number: int | None  # LaTeX counter value, ``None`` without ``\caption``
    caption: tuple[int, int] | None  # caption text span


@dataclass(frozen=True)
class _Numbering:
    """Labels and captions numbered the way LaTeX numbers them (ADR-0056).

    ``labels`` maps a full label name (``fig:cost-matrix``) to its printed
    number (``1``, ``3``, ``2.1``); ``captions`` lists the numbered captions in
    document order; ``floats`` keeps every float environment for the figure pass.
    """

    labels: dict[str, str]
    captions: list[_Caption]
    floats: list[_Float]


def _floats(blanked: str, seeds: dict[str, int] | None = None) -> list[_Float]:
    """Every ``figure``/``table`` environment with its caption and number.

    An uncaptioned float consumes no number, mirroring ``\\refstepcounter``
    (which ``\\caption`` calls); a ``\\caption*`` is unnumbered by definition.
    ``seeds`` starts the per-kind counters at a value other than zero, for
    floats the compiled document numbered before the text handed to pandoc
    (a stripped title page's table; ADR-0059).
    """
    counters = {"figure": 0, "table": 0}
    if seeds:
        for kind, value in seeds.items():
            if kind in counters:
                counters[kind] = value
    floats: list[_Float] = []
    for match in _FLOAT_ENV_RE.finditer(blanked):
        kind = match.group("env")
        inner_start, inner_end = match.start("body"), match.end("body")
        if inner_start < inner_end and blanked[inner_start] == "[":
            close = blanked.find("]", inner_start)
            if close != -1:
                inner_start = close + 1
        caption = _CAPTION_CMD_RE.search(blanked, inner_start, inner_end)
        number: int | None = None
        span: tuple[int, int] | None = None
        if caption is not None and not caption.group("star"):
            bounds = _arg_bounds(blanked, caption.end())
            if bounds is not None:
                counters[kind] += 1
                number = counters[kind]
                span = (bounds[0] + 1, bounds[1] - 1)
        floats.append(
            _Float(kind, match.start(), match.end(), inner_start, inner_end, number, span)
        )
    return floats


def _section_string(state: list[int]) -> str | None:
    """``state`` (chapter/section/subsection/subsubsection) as ``"2.1"``.

    Numbering starts at the first *used* level, so an article-class document
    (no ``\\chapter``) prints ``3`` rather than ``0.3``.
    """
    used = [index for index, value in enumerate(state) if value]
    if not used:
        return None
    return ".".join(str(state[i]) for i in range(used[0], used[-1] + 1))


def _numbering(blanked: str, seeds: dict[str, int] | None = None) -> _Numbering:
    """Number labels and captions the way the compiled PDF does (ADR-0056).

    Sections count level by level (``2``, ``2.1``); a label inside a float takes
    that float's number even when it precedes the ``\\caption``; numbered
    display-math environments count in order. This is what makes ``\\autoref``
    into ``Section 3`` / ``Figure 1`` / ``Table 4`` / ``Equation 2`` without
    asking pandoc (whose counters cannot see a label that a replaced figure
    environment removed, nor number an equation). ``seeds`` continues the float
    counters from a preceding scan (ADR-0059).
    """
    floats = _floats(blanked, seeds)
    captions = [
        _Caption(float_.kind, float_.number, float_.caption[0], float_.caption[1])
        for float_ in floats
        if float_.number is not None and float_.caption is not None
    ]
    numbered = sorted(
        (float_ for float_ in floats if float_.number is not None),
        key=lambda float_: float_.start,
    )
    labels: dict[str, str] = {}
    state = [0, 0, 0, 0]
    equation = 0
    float_index = 0
    for match in _CMD_RE.finditer(blanked):
        cmd = match.group("cmd")
        star = bool(match.group("star"))
        if cmd == "begin":
            env = (_arg_at(blanked, match.end()) or "").strip()
            if env in _NUMBERED_EQ_ENVS and not star:
                equation += 1
            continue
        if cmd in ("end", "caption"):
            continue
        if cmd in _SECTION_LEVELS and not star:
            level = _SECTION_LEVELS[cmd]
            state[level] += 1
            for deeper in range(level + 1, len(state)):
                state[deeper] = 0
            continue
        if cmd != "label":
            continue
        name = (_arg_at(blanked, match.end()) or "").strip()
        if not name:
            continue
        prefix = name.split(":", 1)[0].lower()
        while float_index < len(numbered) and numbered[float_index].end <= match.start():
            float_index += 1
        inside = (
            numbered[float_index]
            if float_index < len(numbered)
            and numbered[float_index].start <= match.start() < numbered[float_index].end
            else None
        )
        value: str | None = None
        if prefix in ("fig", "figure"):
            value = str(inside.number) if inside and inside.kind == "figure" else None
        elif prefix in ("tab", "table"):
            value = str(inside.number) if inside and inside.kind == "table" else None
        elif prefix in _SECTION_PREFIXES:
            value = _section_string(state)
        elif prefix in ("eq", "equation", "eqn"):
            value = str(equation) if equation else None
        if value is not None and name not in labels:
            labels[name] = value
    return _Numbering(labels, captions, floats)


def _ref_word(label: str) -> str | None:
    """The ``\\autoref`` word for a label's prefix, or ``None`` when unknown."""
    if ":" not in label:
        return None
    return _REF_WORDS.get(label.split(":", 1)[0].lower())


def _ref_edits(blanked: str, numbers: _Numbering) -> list[tuple[int, int, str]]:
    """Edits turning ``\\autoref``/``\\ref``/``\\eqref`` into plain text (ADR-0056).

    ``\\ref`` prints the bare number (what the compiled PDF shows),
    ``\\autoref``/``\\cref`` print ``"<Word> N"`` from the label prefix, and
    ``\\eqref`` prints ``"(N)"``. A label with an unknown prefix or no known
    number is left to pandoc, which emits the raw anchor form.
    """
    edits: list[tuple[int, int, str]] = []
    for match in _REF_CMD_RE.finditer(blanked):
        label = match.group("label")
        word = _ref_word(label)
        number = numbers.labels.get(label)
        if word is None or number is None:
            continue
        cmd = match.group("cmd")
        if cmd == "eqref":
            replacement = f"({number})"
        elif cmd == "ref":
            replacement = number
        else:
            replacement = f"{word} {number}"
        edits.append((match.start(), match.end(), replacement))
    return edits


def _rewrite_md_refs(md: str, numbers: _Numbering) -> str:
    """Normalize a cross-reference link pandoc resolved on its own.

    The source-level rewrite above covers every reference the scanner knows;
    this is the safety net for one that slipped through. Only a link pandoc
    marked as a reference (``reference-type="…"``) is rewritten, so an
    author-written link is left alone. ``\\pageref``/``\\nameref`` (which pandoc
    drops anyway) and anything non-numeric are left as-is.
    """

    def repl(match: re.Match[str]) -> str:
        attrs = match.group("attrs")
        if not attrs:
            return match.group(0)
        kind = re.search(r'reference-type="([^"]+)"', attrs)
        if kind is None or kind.group(1) == "pageref":
            return match.group(0)
        label = match.group("label")
        word = _ref_word(label)
        number = numbers.labels.get(label)
        text = match.group("text").strip()
        if word is None or number is None or not re.fullmatch(r"[\d.]+", text):
            return match.group(0)
        if kind.group(1) == "eqref":
            return f"({number})"
        if kind.group(1) == "ref":
            return number
        return f"{word} {number}"

    return _MD_REF_RE.sub(repl, md)


def _apply_edits(text: str, edits: list[tuple[int, int, str]]) -> str:
    """Apply ``(start, end, replacement)`` edits right-to-left, skipping overlaps."""
    limit = len(text)
    for start, end, replacement in sorted(
        edits, key=lambda edit: (edit[0], edit[1]), reverse=True
    ):
        if end > limit:
            continue
        text = text[:start] + replacement + text[end:]
        limit = start
    return text


def _table_caption_edits(numbers: _Numbering) -> list[tuple[int, int, str]]:
    """Edits prefixing every numbered table caption with ``Table N: ``.

    pandoc emits a table caption as a ``: <caption>`` line after the table;
    numbering it in the source keeps the caption and the ``\\autoref`` text
    consistent (both come from the same scan).
    """
    return [
        (caption.arg_start, caption.arg_start, f"Table {caption.number}: ")
        for caption in numbers.captions
        if caption.kind == "table"
    ]


@dataclass
class _Figure:
    """One figure the converter owns, keyed by the marker pandoc will emit."""

    marker: str  # ``ptmfig-N`` image path in the flattened source
    number: int | None  # LaTeX figure number (``None``: no caption)
    label: str | None  # ``\label`` name, for warnings
    kind: str  # "render" (TikZ/pgfplots body) | "file" (project image)
    body: str = ""  # LaTeX rendered through standalone + pdflatex
    source: Path | None = None  # project image (kind == "file")
    labels: list[str] = field(default_factory=list)  # TikZ text (ADR-0060)


# A TikZ/pgfplots figure's text is extracted from its source so it survives
# when no image is written (ADR-0060). The marker stays a plain paragraph.
_FIGURE_NOTE = "ptmfignote-{n}"
_FIGURE_NOTE_RE = re.compile(r"^ptmfignote-(?P<n>\d+)$", re.MULTILINE)

_TIKZ_MATH_RE = re.compile(r"\$[^$]*\$")


def _skip_space(text: str, index: int) -> int:
    """Index of the first non-space character at or after ``index``."""
    while index < len(text) and text[index] in " \t\n\r":
        index += 1
    return index


def _skip_balanced(text: str, index: int, open_ch: str, close_ch: str) -> int:
    """Index just past the ``close_ch`` matching the ``open_ch`` at ``index``."""
    depth = 0
    while index < len(text):
        if text[index] == open_ch:
            depth += 1
        elif text[index] == close_ch:
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return len(text)


def _tikz_text(raw: str) -> str:
    """One node/axis label as plain inline text, keeping ``$…$`` math intact."""
    math: list[str] = []

    def stash(match: re.Match[str]) -> str:
        math.append(match.group(0))
        return f"\x00{len(math) - 1}\x00"

    text = _TIKZ_MATH_RE.sub(stash, raw)
    text = text.replace("\\\\", "; ")
    text = re.sub(r"\\[ ,;!:~]", " ", text)
    for _ in range(3):
        unwrapped = re.sub(r"\\[a-zA-Z@]+\*?\s*\{([^{}]*)\}", r"\1", text)
        if unwrapped == text:
            break
        text = unwrapped
    text = re.sub(r"\\[a-zA-Z@]+\*?", " ", text)
    text = re.sub(r"[{}]", "", text)
    text = re.sub(r";\s*$", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\x00(\d+)\x00", lambda m: math[int(m.group(1))], text)


def _tikz_labels(body: str) -> list[str]:
    """The text content of a TikZ/pgfplots body, in document order (ADR-0060).

    Every ``\\node`` command and every ``node[…]`` attachment on a path is
    collected — boxes, box text, edge labels — so a diagram whose image is not
    written still contributes its content to the Markdown. pgfplots axis labels
    and ``\\legend`` entries are picked up too. Inline ``$…$`` math is kept.
    """
    labels: list[str] = []
    for match in re.finditer(r"\bnode\b", body):
        start = match.start()
        previous = start - 1
        while previous >= 0 and body[previous] in " \t\n\r":
            previous -= 1
        if previous < 0:
            continue
        if body[previous] == "\\":
            if previous != start - 1:
                continue
        elif body[previous] not in ")-":
            continue
        index = _skip_space(body, match.end())
        if index < len(body) and body[index] == "[":
            index = _skip_balanced(body, index, "[", "]")
        while True:
            index = _skip_space(body, index)
            if body.startswith("at", index) and not re.match(
                r"[a-zA-Z]", body[index + 2 : index + 3]
            ):
                index += 2
                continue
            if index < len(body) and body[index] == "(":
                index = _skip_balanced(body, index, "(", ")")
                continue
            break
        index = _skip_space(body, index)
        if index >= len(body) or body[index] != "{":
            continue
        end = _balanced_end(body, index)
        text = _tikz_text(body[index + 1 : end - 1])
        if text:
            labels.append(text)
    for assign in re.finditer(r"\b(?:xlabel|ylabel|zlabel)\s*=\s*\{", body):
        end = _balanced_end(body, assign.end() - 1)
        text = _tikz_text(body[assign.end() : end - 1])
        if text:
            labels.append(text)
    for legend in re.finditer(r"\\legend\s*\{", body):
        end = _balanced_end(body, legend.end() - 1)
        for entry in body[legend.end() : end - 1].split(","):
            text = _tikz_text(entry)
            if text:
                labels.append(text)
    return labels


def _figure_note(figure: _Figure) -> str:
    """The note line standing in for an unwritten figure image (ADR-0060)."""
    return f"*Figure content (from the TikZ source):* {' · '.join(figure.labels)}"


def _figures_enabled() -> bool:
    """Whether the figure pipeline runs (``LATEX_FIGURES=off`` disables it).

    Read lazily like ``IMAGE_LINKS``/``PDF_MODE``, so an engine ``--env`` flip
    applies to the next conversion. Off restores the pre-ADR-0057 behaviour:
    pandoc sees the raw TikZ source and emits a caption-only ``<figure>`` stub.
    """
    return (os.environ.get("LATEX_FIGURES", "on").strip().lower() or "on") in {
        "1",
        "true",
        "yes",
        "on",
    }


def _float_label(blanked: str, float_: _Float) -> str | None:
    """The first ``\\label{…}`` name inside a float, if any."""
    match = _LABEL_CMD_RE.search(blanked, float_.inner_start, float_.inner_end)
    if match is None:
        return None
    return (_arg_at(blanked, match.end()) or "").strip() or None


def _float_removals(blanked: str, float_: _Float) -> list[tuple[int, int]]:
    """Spans of caption/label/spacing commands to drop before rendering a float."""
    spans: list[tuple[int, int]] = []
    for match in _CAPTION_CMD_RE.finditer(blanked, float_.inner_start, float_.inner_end):
        bounds = _arg_bounds(blanked, match.end())
        spans.append((match.start(), bounds[1] if bounds else match.end()))
    for match in _LABEL_CMD_RE.finditer(blanked, float_.inner_start, float_.inner_end):
        bounds = _arg_bounds(blanked, match.end())
        spans.append((match.start(), bounds[1] if bounds else match.end()))
    for match in _CAPTION_SKIP_RE.finditer(blanked, float_.inner_start, float_.inner_end):
        spans.append((match.start(), match.end()))
    return spans


def _strip_spans(text: str, spans: list[tuple[int, int]]) -> str:
    """Remove ``(start, end)`` spans from ``text`` (right-to-left)."""
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + text[end:]
    return text


def _includegraphics_path(blanked: str, index: int) -> str | None:
    """The path argument of the ``\\includegraphics`` starting at ``index``."""
    i = index
    while i < len(blanked) and blanked[i] in " \t\n\r":
        i += 1
    if i < len(blanked) and blanked[i] == "[":
        close = blanked.find("]", i)
        if close != -1:
            i = close + 1
    return _arg_at(blanked, i)


def _figure_image(blanked: str, float_: _Float) -> str | None:
    """The single project image a non-TikZ figure references, if any.

    ``None`` for a figure with zero or several ``\\includegraphics`` (e.g.
    subfigures); such a figure is left to pandoc as before.
    """
    matches = list(_INCLUDEGRAPHICS_RE.finditer(blanked, float_.inner_start, float_.inner_end))
    if len(matches) != 1:
        return None
    return _includegraphics_path(blanked, matches[0].end())


def _replace_figures(
    text: str,
    blanked: str,
    numbers: _Numbering,
    project_dir: Path,
    warnings: list[str],
) -> tuple[str, list[_Figure]]:
    """Swap figure environments for an image marker plus a numbered caption.

    A figure whose body holds a ``tikzpicture`` or a pgfplots axis is rendered
    from source (ADR-0057); one that references a single project image keeps
    that image; either way the caption becomes ``\\textbf{Figure N: …}`` so
    pandoc turns it into a bold numbered line with real ``$…$`` math instead of
    a captionless ``<figure>`` stub with MathML. Figures with several images
    (subfigures) are left untouched, with their caption numbered in place.
    """
    edits: list[tuple[int, int, str]] = []
    figures: list[_Figure] = []
    for float_ in numbers.floats:
        if float_.kind != "figure":
            continue
        label = _float_label(blanked, float_)
        source: Path | None = None
        if _TIKZ_RE.search(blanked, float_.inner_start, float_.inner_end):
            kind = "render"
        else:
            url = _figure_image(blanked, float_)
            if url is None:
                prefix = _caption_prefix(float_)
                if prefix:
                    edits.append(prefix)
                continue
            source = _resolve_image(project_dir, url)
            if source is None:
                warnings.append(f"Could not resolve figure image: {url}")
                prefix = _caption_prefix(float_)
                if prefix:
                    edits.append(prefix)
                continue
            kind = "file"
        marker = _FIGURE_MARKER.format(n=len(figures) + 1)
        caption = ""
        if float_.caption is not None and float_.number is not None:
            caption_text = text[float_.caption[0] : float_.caption[1]].strip()
            caption = f"\n\n\\textbf{{Figure {float_.number}: {caption_text}}}"
        body = ""
        labels: list[str] = []
        if kind == "render":
            removals = [
                (start - float_.inner_start, end - float_.inner_start)
                for start, end in _float_removals(blanked, float_)
            ]
            body = _strip_spans(text[float_.inner_start : float_.inner_end], removals)
            labels = _tikz_labels(body)
        note = f"\n\n{_FIGURE_NOTE.format(n=len(figures) + 1)}" if labels else ""
        figures.append(
            _Figure(marker, float_.number, label, kind, body, source, labels)
        )
        edits.append(
            (float_.start, float_.end, f"\n\n\\includegraphics{{{marker}}}{caption}{note}")
        )
    return _apply_edits(text, edits), figures


def _caption_prefix(float_: _Float) -> tuple[int, int, str] | None:
    """Number a figure caption left inside its environment (``Figure N: ``)."""
    if float_.caption is None or float_.number is None:
        return None
    return (float_.caption[0], float_.caption[0], f"Figure {float_.number}: ")


def _project_style_lines(project_dir: Path) -> str:
    """TikZ/PGF declarations from the project's own ``.sty``/``.tex`` files.

    A figure often leans on styles the project defines once (``packages.sty``'s
    ``\\tikzset{block/.style=…}`` or ``\\usetikzlibrary{…}``); those files are
    ``\\usepackage``'d rather than flattened, so the standalone render would
    miss them. The declarations are collected verbatim and injected into the
    figure preamble (ADR-0057); a project that needs more can point
    ``LATEX_FIGURE_PREAMBLE`` at its own preamble file.
    """
    lines: list[str] = []
    seen: set[str] = set()
    files = sorted(project_dir.rglob("*.sty")) + sorted(project_dir.rglob("*.tex"))
    for path in files[:200]:
        try:
            if path.stat().st_size > 262_144:
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        blanked = _blank_latex(content)
        for match in _STYLE_CMD_RE.finditer(blanked):
            command = match.group(0).lstrip("\\")
            end = match.end()
            for _ in range(_STYLE_ARG_COUNT.get(command, 1)):
                bounds = _arg_bounds(blanked, end)
                if bounds is None:
                    break
                end = bounds[1]
            else:
                snippet = re.sub(r"\s+", " ", content[match.start() : end]).strip()
                if snippet and snippet not in seen:
                    seen.add(snippet)
                    lines.append(snippet)
    return "\n".join(lines)


def _figure_preamble(project_dir: Path, warnings: list[str]) -> str:
    """The standalone preamble used to render TikZ/pgfplots figures (ADR-0057).

    Defaults first, then the project's own style declarations (so they win a
    clash), then ``LATEX_FIGURE_PREAMBLE`` (a file, last word).
    """
    parts = [_FIGURE_PREAMBLE_DEFAULT, _project_style_lines(project_dir)]
    extra = os.environ.get("LATEX_FIGURE_PREAMBLE", "").strip()
    if extra:
        try:
            parts.append(Path(extra).expanduser().read_text(encoding="utf-8"))
        except OSError as exc:
            warnings.append(f"Could not read LATEX_FIGURE_PREAMBLE={extra}: {exc}")
    return "\n".join(part for part in parts if part.strip()) + "\n"


def _render_figure_png(body: str, project_dir: Path, preamble: str) -> bytes | None:
    """Render one figure body to PNG bytes, or ``None`` on failure.

    Mirrors :func:`_render_math_png` (temp dir, ``SOURCE_DATE_EPOCH`` for
    reproducible PDFs, PyMuPDF 2× pixmap) with the project directory on the TeX
    search path so a figure's own ``\\includegraphics`` resolves. ``-shell-escape``
    is never passed, so a project's ``\\write18`` cannot execute.
    """
    doc = f"{preamble}\\begin{{document}}\n{body.strip()}\n\\end{{document}}\n"
    try:
        with tempfile.TemporaryDirectory(prefix="ptm-figure-") as tmp:
            tmpd = Path(tmp)
            (tmpd / "fig.tex").write_text(doc, encoding="utf-8")
            env = dict(os.environ, SOURCE_DATE_EPOCH="0")
            tex_inputs = f"{project_dir}{os.pathsep}" + env.get("TEXINPUTS", "")
            if not tex_inputs.endswith(os.pathsep):
                tex_inputs += os.pathsep
            env["TEXINPUTS"] = tex_inputs
            proc = subprocess.run(
                [PDLATEX_PATH, "-interaction=nonstopmode", "-halt-on-error", "fig.tex"],
                cwd=str(tmpd),
                capture_output=True,
                text=True,
                timeout=180,
                env=env,
            )
            if proc.returncode != 0:
                detail = _latex_error(proc.stdout or proc.stderr or "")
                if detail:
                    raise RuntimeError(detail)
                return None
            pdf = tmpd / "fig.pdf"
            if not pdf.exists():
                return None
            doc_pdf = fitz.open(pdf)
            try:
                return doc_pdf[0].get_pixmap(matrix=fitz.Matrix(2, 2)).tobytes("png")
            finally:
                doc_pdf.close()
    except Exception:
        return None


def _latex_error(log: str) -> str:
    """The first TeX error line in a pdflatex log, for a warning message."""
    for line in log.splitlines():
        if line.startswith("!"):
            return line.strip()[:120]
    return ""


def _read_figure_image(path: Path, warnings: list[str]) -> tuple[bytes, str] | None:
    """Read a figure image as ``(bytes, extension)``; PDFs are rasterized."""
    ext = path.suffix.lower().lstrip(".")
    if ext == "pdf":
        try:
            doc = fitz.open(path)
            try:
                png = doc[0].get_pixmap(matrix=fitz.Matrix(2, 2)).tobytes("png")
            finally:
                doc.close()
            return png, "png"
        except Exception as exc:  # noqa: BLE001 - any failure drops the image
            warnings.append(f"Could not rasterize figure image {path.name}: {exc}")
            return None
    if ext in {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff"}:
        try:
            return path.read_bytes(), ext
        except OSError as exc:
            warnings.append(f"Could not read figure image {path}: {exc}")
            return None
    warnings.append(f"Unsupported figure image format .{ext}: {path.name}")
    return None


def _figure_name(figure: _Figure) -> str:
    """``Figure 2 (fig:x)`` — how a figure is named in warnings."""
    head = f"Figure {figure.number}" if figure.number else "Figure"
    return f"{head} ({figure.label})" if figure.label else head


def _replace_figure_markers(
    md: str,
    figures: list[_Figure],
    project_dir: Path,
    assets_dir: Path | None,
    stem: str,
    counter: list[int] | None,
    dedup: dict[str, str] | None,
    warnings: list[str],
) -> str:
    """Turn the ``![](ptmfig-N)`` markers into the figure's image (or a note).

    With image links on, each figure's bytes — the rendered TikZ body or the
    referenced project image — are written through :func:`write_image`
    (content-hash deduplicated, sharing the counter with the other assets) and
    referenced as ``![Figure N](assets/<stem>/…)``. With image links off the
    marker is dropped and the numbered caption line pandoc emitted stands
    alone, with one warning per figure — but a TikZ figure then keeps its text
    as a ``*Figure content …*`` note (ADR-0060). Rendering is best-effort: a
    missing pdflatex or a failed compile falls back to the same note and never
    fails the conversion.
    """
    if not figures:
        return md

    def notes(md_text: str, written: dict[str, str]) -> str:
        """Replace the note markers with the unwritten figures' text."""
        wanted = {
            figure.marker: _figure_note(figure)
            for figure in figures
            if figure.labels and figure.marker not in written
        }
        return _FIGURE_NOTE_RE.sub(
            lambda match: wanted.get(f"ptmfig-{match.group('n')}", ""), md_text
        )

    by_marker = {figure.marker: figure for figure in figures}
    if assets_dir is None:
        for figure in figures:
            warnings.append(f"{_figure_name(figure)}: image omitted (IMAGE_LINKS=off)")
        md = notes(md, {})
        return re.sub(r"\n{3,}", "\n\n", _FIGURE_MARKER_RE.sub("", md))

    can_render = latex_available()
    preamble = ""
    if any(figure.kind == "render" for figure in figures):
        if can_render:
            preamble = _figure_preamble(project_dir, warnings)
        else:
            warnings.append("pdflatex not available; TikZ figures kept as captions only")

    written: dict[str, str] = {}
    for figure in figures:
        blob: bytes | None = None
        ext = "png"
        if figure.kind == "render":
            if not can_render:
                continue
            blob = _render_figure_png(figure.body, project_dir, preamble)
            if blob is None:
                warnings.append(f"{_figure_name(figure)}: could not render; image omitted")
                continue
        elif figure.source is not None:
            loaded = _read_figure_image(figure.source, warnings)
            if loaded is None:
                continue
            blob, ext = loaded
        if blob is None:
            continue
        filename = write_image(blob, ext, assets_dir, stem, counter, warnings, dedup)
        if filename is not None:
            written[figure.marker] = _link_dest(f"assets/{stem}/{filename}")

    def repl(match: re.Match[str]) -> str:
        figure = by_marker.get(match.group("marker"))
        dest = written.get(match.group("marker"))
        if figure is None or dest is None:
            return ""
        alt = f"Figure {figure.number}" if figure.number else "figure"
        return f"![{alt}]({dest})"

    md = notes(md, written)
    return re.sub(r"\n{3,}", "\n\n", _FIGURE_MARKER_RE.sub(repl, md))


def _find_bibs(source: str, project_dir: Path) -> list[Path]:
    """Return absolute ``.bib`` paths referenced by or present in the project."""
    bibs: list[Path] = []
    seen: set[str] = set()
    for m in _BIB_CMD_RE.finditer(source):
        for name in re.split(r",", m.group(1)):
            name = name.strip()
            if not name:
                continue
            p = Path(name)
            if not p.suffix:
                p = p.with_suffix(".bib")
            p = (project_dir / p).resolve() if not p.is_absolute() else p.resolve()
            if p.is_file() and str(p) not in seen:
                bibs.append(p)
                seen.add(str(p))
    for p in sorted(project_dir.glob("*.bib")):
        p = p.resolve()
        if p.is_file() and str(p) not in seen:
            bibs.append(p)
            seen.add(str(p))
    return bibs


# The bibliography style (an author-year rendering close to biblatex's
# ``authoryear``): source-case titles, ``In:``/``pp.``, expanded page ranges,
# ``(visited on …)``, publisher places and ``et al.``; ADR-0061. Written into
# pandoc's temp dir and passed with ``--csl``.
_AUTHOR_YEAR_CSL = r"""<?xml version="1.0" encoding="utf-8"?>
<style xmlns="http://purl.org/net/xbiblio/csl" class="in-text" version="1.0"
       demote-non-dropping-particle="never" page-range-format="expanded"
       default-locale="en-GB">
  <info>
    <title>PresentationToMarkdown author-year</title>
    <title-short>PTM author-year</title-short>
    <id>https://github.com/presentation-to-markdown/csl/author-year</id>
    <updated>2026-10-15T00:00:00+00:00</updated>
    <category citation-format="author-date"/>
  </info>

  <macro name="author">
    <names variable="author">
      <name name-as-sort-order="first" sort-separator=", " delimiter=", "
            and="text" delimiter-precedes-last="never"
            et-al-min="4" et-al-use-first="1"/>
      <substitute>
        <names variable="editor"/>
        <text variable="title"/>
      </substitute>
    </names>
  </macro>

  <macro name="author-short">
    <names variable="author">
      <name form="short" delimiter=", " and="text" delimiter-precedes-last="never"
            et-al-min="3" et-al-use-first="1"/>
      <substitute>
        <names variable="editor"/>
        <text variable="title" form="short"/>
      </substitute>
    </names>
  </macro>

  <macro name="year">
    <date variable="issued">
      <date-part name="year"/>
    </date>
  </macro>

  <macro name="url">
    <choose>
      <if variable="doi">
        <group delimiter=" ">
          <text value="doi:"/>
          <text variable="doi"/>
        </group>
      </if>
      <else-if variable="url">
        <group delimiter=" ">
          <text value="url:"/>
          <text variable="url"/>
          <group prefix="(" suffix=")" delimiter=" ">
            <text value="visited on"/>
            <date variable="accessed">
              <date-part name="day" form="ordinal" suffix=" "/>
              <date-part name="month" form="short" suffix=" "/>
              <date-part name="year"/>
            </date>
          </group>
        </group>
      </else-if>
    </choose>
  </macro>

  <macro name="head">
    <group delimiter=" ">
      <text macro="author"/>
      <text macro="year" prefix="(" suffix=")"/>
    </group>
  </macro>

  <citation et-al-min="3" et-al-use-first="1" disambiguate-add-year-suffix="true"
            disambiguate-add-givenname="true" givenname-disambiguation-rule="by-cite">
    <sort>
      <key macro="author-short"/>
      <key variable="issued"/>
    </sort>
    <layout prefix="(" suffix=")" delimiter="; ">
      <group delimiter=" ">
        <text macro="author-short"/>
        <text macro="year"/>
      </group>
    </layout>
  </citation>

  <bibliography et-al-min="4" et-al-use-first="1" subsequent-author-substitute="&#8212;"
                subsequent-author-substitute-rule="complete-all">
    <sort>
      <key macro="author"/>
      <key variable="issued"/>
      <key variable="title"/>
    </sort>
    <layout suffix=".">
      <choose>
        <if type="book">
          <group delimiter=". ">
            <text macro="head"/>
            <text variable="title" font-style="italic"/>
            <group delimiter=" ">
              <number variable="edition" form="ordinal"/>
              <text value="ed."/>
            </group>
            <group delimiter=": ">
              <text variable="publisher-place"/>
              <text variable="publisher"/>
            </group>
            <text macro="url"/>
          </group>
        </if>
        <else-if type="article-journal article-magazine article-newspaper">
          <group delimiter=". ">
            <text macro="head"/>
            <text variable="title" quotes="true"/>
            <group delimiter=", ">
              <group delimiter=" ">
                <text value="In:"/>
                <text variable="container-title" font-style="italic"/>
                <group delimiter=".">
                  <text variable="volume"/>
                  <text variable="issue"/>
                </group>
              </group>
              <text variable="page"/>
            </group>
            <text macro="url"/>
          </group>
        </else-if>
        <else-if type="paper-conference chapter">
          <group delimiter=". ">
            <text macro="head"/>
            <text variable="title" quotes="true"/>
            <group delimiter=", ">
              <group delimiter=" ">
                <text value="In:"/>
                <text variable="container-title" font-style="italic"/>
              </group>
              <group delimiter=" ">
                <label variable="page" form="short"/>
                <text variable="page"/>
              </group>
            </group>
            <text macro="url"/>
          </group>
        </else-if>
        <else>
          <group delimiter=". ">
            <text macro="head"/>
            <text variable="title"/>
            <text macro="url"/>
          </group>
        </else>
      </choose>
    </layout>
  </bibliography>
</style>
"""

# BibTeX fields pandoc's reader sentence-cases when they are not protected
# with braces, so the author's capitalisation would be lost (ADR-0061).
_BIB_PROTECT_FIELDS = r"title|booktitle|series|subtitle"

_BIB_FIELD_RE = re.compile(
    r"(?:(?<=\n)|(?<=[{,]))\s*(" + _BIB_PROTECT_FIELDS + r")\s*=\s*",
    re.IGNORECASE,
)


def _bib_value_end(text: str, start: int) -> tuple[int, int] | None:
    """Content span of a BibTeX field value at ``start`` (quoted or braced)."""
    if start >= len(text):
        return None
    if text[start] == '"':
        index = start + 1
        while index < len(text):
            if text[index] == "\\":
                index += 2
                continue
            if text[index] == '"':
                return start + 1, index
            index += 1
        return None
    if text[start] == "{":
        end = _balanced_end(text, start)
        return start + 1, end - 1
    return None


def _protect_bib_cases(text: str) -> str:
    """Brace-protect case-sensitive BibTeX values before pandoc reads them.

    pandoc's BibTeX reader sentence-cases unprotected ``title``/``booktitle``/
    ``series`` values, so even a bibliography style that never title-cases would
    print ``The Foundations of Cost-Sensitive Learning`` as ``The foundations
    of cost-sensitive learning`` — the author's capitals removed. Wrapping the
    value in a brace group preserves the case exactly; the value's content is
    unchanged (ADR-0061).
    """
    out: list[str] = []
    index = 0
    while True:
        match = _BIB_FIELD_RE.search(text, index)
        if match is None:
            out.append(text[index:])
            break
        value_start = match.end()
        if value_start < len(text) and text[value_start] == '"':
            bounds = _bib_value_end(text, value_start)
            if bounds is not None:
                content = text[bounds[0] : bounds[1]]
                if content.startswith("{") and content.endswith("}"):
                    out.append(text[index : bounds[1] + 1])
                else:
                    out.append(text[index : bounds[0]] + "{" + content + "}" + '"')
                index = bounds[1] + 1
                continue
        elif value_start < len(text) and text[value_start] == "{":
            bounds = _bib_value_end(text, value_start)
            if bounds is not None:
                content = text[bounds[0] : bounds[1]]
                if content.startswith("{") and content.endswith("}"):
                    out.append(text[index : bounds[1] + 1])
                else:
                    out.append(text[index : value_start] + "{{" + content + "}}")
                index = bounds[1] + 1
                continue
        out.append(text[index:value_start])
        index = value_start
    return "".join(out)


def _bibliography_style(warnings: list[str]) -> str:
    """The CSL pandoc renders citations and the bibliography with (ADR-0061).

    ``LATEX_CSL=<file.csl>`` replaces the bundled author-year style, like
    ``LATEX_FIGURE_PREAMBLE`` does for figures. Read lazily so an engine
    ``--env`` flip applies to the next conversion.
    """
    override = os.environ.get("LATEX_CSL", "").strip()
    if override:
        try:
            return Path(override).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            warnings.append(f"Could not read LATEX_CSL={override}: {exc}")
    return _AUTHOR_YEAR_CSL


def _run_pandoc(
    body: str,
    project_dir: Path,
    bibs: list[Path],
    warnings: list[str],
) -> str:
    """Run pandoc on the flattened body; return the Markdown.

    The bundled author-year CSL is passed for citations and the bibliography
    (ADR-0061), and a case-protected copy of every ``.bib`` is handed to pandoc
    (the real files are never modified). Raises :class:`RuntimeError` when
    pandoc itself fails (timeout / non-zero exit), so the caller surfaces the
    pandoc message as the conversion error.
    """
    with tempfile.TemporaryDirectory(prefix="ptm-latex-") as tmp:
        tmpd = Path(tmp)
        flat = tmpd / "flattened.tex"
        flat.write_text(body, encoding="utf-8")
        out = tmpd / "out.md"
        csl = tmpd / "author-year.csl"
        csl.write_text(_bibliography_style(warnings), encoding="utf-8")
        cmd = [
            PANDOC_PATH,
            "-f",
            "latex",
            "-t",
            "markdown+tex_math_dollars+pipe_tables-simple_tables-multiline_tables-grid_tables",
            "--wrap=none",
            "--shift-heading-level-by=1",
            "--citeproc",
            "--csl",
            str(csl),
        ]
        for index, bib in enumerate(bibs):
            try:
                content = bib.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                warnings.append(f"Could not read bibliography {bib}: {exc}")
                continue
            protected = tmpd / f"bib-{index}.bib"
            protected.write_text(_protect_bib_cases(content), encoding="utf-8")
            cmd += ["--bibliography", str(protected)]
        cmd += [str(flat), "-o", str(out)]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(project_dir),
                capture_output=True,
                text=True,
                timeout=600,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("pandoc timed out") from None
        if proc.returncode != 0:
            raise RuntimeError(f"pandoc failed: {(proc.stderr or '').strip()[:300]}")
        if proc.stderr.strip():
            warnings.append(f"pandoc: {proc.stderr.strip().splitlines()[0]}")
        return out.read_text(encoding="utf-8")


def _strip_attrs(md: str) -> str:
    """Strip pandoc's attributes: link/span attribute sets and line-end ids.

    A heading (``## Data strategy {#sec:data}``), a table caption line
    (``: Business KPIs {#tab:x}``) and citeproc's case-preserving spans
    (``[scikit-learn developers]{.nocase}``) all carry one; Obsidian renders
    them raw, and the cross-references those anchors served are plain text now
    (ADR-0056).
    """
    md = _ATTR_RE.sub(r"\1", md)
    md = _SPAN_ATTR_RE.sub(r"\1", md)
    return _LINE_ATTR_RE.sub("", md)


def _bold_table_captions(md: str) -> str:
    """Render pandoc's ``: Table N: …`` caption lines as ``**Table N: …**``.

    The number was added in the source (:func:`_table_caption_edits`); bolding
    here makes the line read like the figures' numbered captions (ADR-0056).
    """
    return _MD_TABLE_CAPTION_RE.sub(lambda m: f"**{m.group('caption')}**", md)


def _cleanup_bibliography(md: str) -> str:
    """Convert pandoc's citeproc bibliography divs into a ``## Bibliography`` list.

    ``--citeproc`` renders the reference list as fenced divs (``::: {.csl-entry}``)
    that most Markdown renderers show raw. Replace each entry with a bullet and the
    enclosing ``csl-bib-body`` div with a ``## Bibliography`` heading.
    """

    def entry_repl(m: re.Match) -> str:
        text = re.sub(r"\s+", " ", m.group(1)).strip()
        text = re.sub(r"\[([^\]]*)\]\{\.nocase\}", r"\1", text)
        return f"- {text}"

    md = _CIT_ENTRY_RE.sub(entry_repl, md)
    md = _BIB_BODY_RE.sub("## Bibliography", md)
    md = _FENCE_RE.sub("", md)
    return md


def _polish_bibliography(md: str) -> str:
    """Restore the dashes the Markdown writer escaped in the bibliography.

    pandoc writes an en dash as ``--`` and the em dash of a repeated-author
    substitute as ``---``; Obsidian shows both literally. Only the bibliography
    block is touched, and only where the meaning is unambiguous: a ``--``
    between digits is a page range, a leading ``---`` a repeated author
    (ADR-0061).
    """
    start = md.rfind("## Bibliography")
    if start < 0:
        return md
    head, block = md[:start], md[start:]
    block = re.sub(r"(?<=\d)--(?=\d)", "\u2013", block)
    block = re.sub(r"(?m)^(\s*-\s+)---(?=\s)", "\\1\u2014", block)
    return head + block


_MATH_SPAN_RE = re.compile(r"\$\$.*?\$\$|\$[^$\n]*\$", re.DOTALL)


def _unescape_literals(md: str) -> str:
    """Undo pandoc's over-escaping of literal text outside math (ADR-0058).

    The Markdown writer escapes ``~`` and ``[``/``]`` even where no Markdown
    meaning is at stake (``\\~18/yr`` in a table, ``\\[TBD\\]``), which shows
    the backslash in Obsidian. Only those two unambiguous literal escapes are
    undone, and never inside ``$…$``/``$$…$$`` math, where the backslash is TeX.
    """

    def chunk(text: str) -> str:
        text = re.sub(r"\\~(?![{A-Za-z\\])", "~", text)

        def brackets(match: re.Match[str]) -> str:
            following = text[match.end() : match.end() + 1]
            if following in ("(", "["):
                return match.group(0)
            return f"[{match.group(1)}]"

        return re.sub(r"\\\[([^\[\]\n]{1,60})\\]", brackets, text)

    parts: list[str] = []
    last = 0
    for match in _MATH_SPAN_RE.finditer(md):
        parts.append(chunk(md[last : match.start()]))
        parts.append(match.group(0))
        last = match.end()
    parts.append(chunk(md[last:]))
    return "".join(parts)


def _rewrite_images(
    md: str,
    project_dir: Path,
    assets_dir: Path,
    stem: str,
    counter: list[int],
    dedup: dict[str, str],
    warnings: list[str],
) -> str:
    """Rewrite relative image references into ``assets/<stem>/`` (content-hash dedup)."""

    def repl(m: re.Match) -> str:
        alt = re.sub(r"\{[^{}]*\}", "", m.group("alt"))
        alt = re.sub(r"\\([\[\]])", r"\1", alt)
        url = m.group("url")
        if "://" in url or url.startswith(("#", "data:")):
            return m.group(0)
        src = _resolve_image(project_dir, url)
        if src is None:
            return m.group(0)
        try:
            blob = src.read_bytes()
        except OSError as exc:
            warnings.append(f"Could not read image {src}: {exc}")
            return m.group(0)
        filename = write_image(blob, src.suffix or "bin", assets_dir, stem, counter, warnings, dedup)
        if filename is None:
            return m.group(0)
        return f"![{alt}]({_link_dest(f'assets/{stem}/{filename}')})"

    return _IMAGE_RE.sub(repl, md)


def _strip_images(md: str) -> str:
    """Remove pandoc's figure references from the Markdown (ADR-0047).

    With ``IMAGE_LINKS`` off LaTeX figures are dropped rather than rewritten
    into ``assets/``: LaTeX runs no vision pass (ADR-0038), so there is no AI
    description to keep in their place. Display math is untouched here (it stays
    the ``$$...$$`` passthrough pandoc emitted).
    """
    md = _IMAGE_RE.sub("", md)
    return re.sub(r"\n{3,}", "\n\n", md)


def _resolve_image(project_dir: Path, url: str) -> Path | None:
    """Resolve an image reference against the project dir, tolerating ``../``.

    ``\\import``-imported files can write image paths relative to their own
    subdirectory (e.g. ``../images/x.png``). Resolve against the project root
    first, then strip leading ``../`` components one at a time — mirroring the
    graphics search-path fallback the ``import`` package performs.
    """
    candidates = [url]
    cand = url
    while cand.startswith("../"):
        cand = cand[3:]
        candidates.append(cand)
    for cand in candidates:
        src = (project_dir / cand).resolve()
        if src.is_file():
            return src
    return None


def _render_math_png(expr: str) -> bytes | None:
    """Render one display-math expression to PNG bytes, or ``None`` on failure.

    ``expr`` is the raw ``$$...$$`` body: a plain expression, or a
    ``\\begin{equation}...\\end{equation}``-style block (kept intact, since its
    inner environments — ``cases``, ``array`` — only render inside them).
    """
    body = expr.strip()
    if body.startswith(("\\[", "\\begin{")):
        content = body
    else:
        content = f"\\[{body}\\]"
    doc = (
        "\\documentclass[preview,border=4pt]{standalone}\n"
        "\\usepackage{amsmath,amssymb,bm}\n"
        "\\begin{document}\n"
        f"{content}\n"
        "\\end{document}\n"
    )
    try:
        with tempfile.TemporaryDirectory(prefix="ptm-math-") as tmp:
            tmpd = Path(tmp)
            (tmpd / "eq.tex").write_text(doc, encoding="utf-8")
            env = dict(os.environ, SOURCE_DATE_EPOCH="0")
            proc = subprocess.run(
                [PDLATEX_PATH, "-interaction=nonstopmode", "-halt-on-error", "eq.tex"],
                cwd=str(tmpd),
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )
            pdf = tmpd / "eq.pdf"
            if proc.returncode != 0 or not pdf.exists():
                return None
            doc_pdf = fitz.open(pdf)
            try:
                return doc_pdf[0].get_pixmap(matrix=fitz.Matrix(2, 2)).tobytes("png")
            finally:
                doc_pdf.close()
    except Exception:
        return None


def _render_display_math(
    md: str,
    assets_dir: Path,
    stem: str,
    counter: list[int],
    dedup: dict[str, str],
    warnings: list[str],
) -> str:
    """Render ``$$...$$`` blocks to PNGs; inline ``$...$`` stays as passthrough."""
    if not _DISPLAY_MATH_RE.search(md):
        return md
    if not latex_available():
        warnings.append("pdflatex not available; display math kept as $$...$$")
        return md
    cache: dict[str, str] = {}

    def repl(m: re.Match) -> str:
        expr = m.group("body").strip()
        if not expr:
            return m.group(0)
        filename = cache.get(expr)
        if filename is None:
            png = _render_math_png(expr)
            if png is None:
                warnings.append(f"Could not render display math: {expr[:60]}")
                return m.group(0)
            filename = write_image(png, "png", assets_dir, stem, counter, warnings, dedup)
            if filename is None:
                return m.group(0)
            cache[expr] = filename
        return f"![{expr}]({_link_dest(f'assets/{stem}/{filename}')})"

    return _DISPLAY_MATH_RE.sub(repl, md)


class LatexConverter(Converter):
    extensions = (".tex",)
    project_extensions = (".tex",)
    ai_passes = frozenset({"summary"})
    # A LaTeX project is one continuous document (`# Title` + `##` sections),
    # so the summary pass chunks it by section rather than by slide (ADR-0052).
    chunking = "sections"

    @classmethod
    def project_entry(cls, directory: Path) -> Path | None:
        """The entry ``.tex`` of a LaTeX project, or ``None`` if not one.

        Only ``.tex`` files in the directory *root* are considered (a project's
        entry file carries ``\\documentclass``; a parent-of-projects folder has
        none at its own root). Fallbacks: ``main.tex``, then a sole ``.tex``.
        """
        try:
            root_tex = sorted(
                p for p in directory.iterdir()
                if p.is_file() and p.suffix.lower() == ".tex"
            )
        except OSError:
            return None
        if not root_tex:
            return None
        for p in root_tex:
            try:
                if "\\documentclass" in p.read_text(encoding="utf-8", errors="replace"):
                    return p
            except OSError:
                continue
        for p in root_tex:
            if p.name.lower() == "main.tex":
                return p
        if len(root_tex) == 1:
            return root_tex[0]
        return None

    def convert(
        self,
        path: Path,
        output_dir: Path,
        progress_callback: PageProgressCallback | None = None,
        output_stem: str | None = None,
    ) -> ConvertResult:
        result = ConvertResult(source_path=path)
        try:
            if not pandoc_available():
                result.error = "pandoc is required for LaTeX conversion (install it: brew install pandoc)"
                return result
            if path.is_dir():
                project_dir = path
                entry = self.project_entry(project_dir)
                if entry is None:
                    result.error = f"No .tex source found in {path}"
                    return result
                stem = output_stem or path.resolve().name
            else:
                project_dir = path.parent
                entry = path
                stem = output_stem or path.stem

            source = entry.read_text(encoding="utf-8", errors="replace")
            flattened = _flatten(
                source, entry.parent, {entry.resolve()}, 0, result.warnings
            )

            # The title page is stripped before pandoc, so the title block is
            # derived while it is still there (ADR-0056): \title → the title
            # page's biggest line → the project folder name.
            titlepage = _titlepage_body(flattened)
            title = (
                _strip_latex(_braced(flattened, "title") or "")
                or _title_from_titlepage(titlepage)
                or stem
            )
            authors = _strip_latex(_braced(flattened, "author") or "")

            bibs = _find_bibs(flattened, project_dir)
            # The compiled document numbers the floats in the title page too,
            # so the whole flattened source is scanned for the counters the
            # stripped front matter consumes (ADR-0059).
            full_numbers = _numbering(_blank_latex(flattened))
            seeds = _stripped_float_seeds(flattened, full_numbers)
            front = _front_items(flattened, full_numbers, entry.parent, title)
            body = _strip_frontmatter(flattened)

            # One deterministic scan numbers sections, floats and equations
            # (ADR-0056). Cross-references become plain text and table captions
            # gain their number before pandoc runs, because pandoc cannot see a
            # label a replaced figure environment removed and cannot number an
            # equation at all.
            blanked = _blank_latex(body)
            numbers = _numbering(blanked, seeds)
            # A label only the stripped title page carries (its members table)
            # still has a number, so a \autoref to it resolves.
            labels = dict(full_numbers.labels)
            labels.update(numbers.labels)
            numbers = _Numbering(labels, numbers.captions, numbers.floats)
            body = _apply_edits(
                body, _ref_edits(blanked, numbers) + _table_caption_edits(numbers)
            )
            figures: list[_Figure] = []
            if _figures_enabled():
                # The rewrites shifted offsets: rescan so the figure pass works
                # on spans of the current text (labels and numbers unchanged).
                figure_blanked = _blank_latex(body)
                body, figures = _replace_figures(
                    body, figure_blanked, _numbering(figure_blanked, seeds),
                    project_dir, result.warnings,
                )

            md = _run_pandoc(body, project_dir, bibs, result.warnings)
            md = _rewrite_md_refs(md, numbers)
            md = _strip_attrs(md)
            md = _unescape_literals(md)
            md = _cleanup_bibliography(md)
            md = _polish_bibliography(md)
            md = _bold_table_captions(md)

            if config.image_links_enabled():
                assets_dir = output_dir / "assets" / stem
                assets_dir.mkdir(parents=True, exist_ok=True)
                counter = [1]
                dedup: dict[str, str] = {}
                md = _rewrite_images(md, project_dir, assets_dir, stem, counter, dedup, result.warnings)
                md = _replace_figure_markers(
                    md, figures, project_dir, assets_dir, stem, counter, dedup,
                    result.warnings,
                )
                md = _render_display_math(md, assets_dir, stem, counter, dedup, result.warnings)
            else:
                md = _replace_figure_markers(
                    md, figures, project_dir, None, stem, None, None, result.warnings
                )
                md = _strip_images(md)

            lines = [f"# {title}", ""]
            if authors:
                lines.extend([f"*{authors}*", ""])
            if front:
                lines.append(_front_markdown(front))
                lines.append("")
            lines.append(md.rstrip())
            lines.append("")

            md_path = output_dir / f"{stem}.md"
            md_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
            result.md_path = md_path

            if progress_callback:
                progress_callback(1, 1, path.name)
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        return result
