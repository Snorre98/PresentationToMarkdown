"""LaTeX project -> Markdown converter built on pandoc (ADR-0038).

A LaTeX *project* is a directory containing ``.tex`` source (plus an optional
``.bib`` and image assets). The converter flattens ``\\input``/``\\include``/
``\\import``/``\\subimport`` into a single source, hands it to pandoc, then
post-normalizes the output into the pipeline's conventions:

- a synthetic ``# Title`` + ``*Authors*`` block (``\\title``/``\\author``, else
  the directory name),
- pandoc ``--shift-heading-level-by=1`` so ``\\section`` becomes ``##`` (keeping
  the top-level ``#`` chunking contract for ``format``/``summary``),
- image references rewritten into ``assets/<stem>/`` with content-hash
  deduplication (``base.write_image``),
- display math rendered to a PNG via ``standalone`` + pdflatex + PyMuPDF;
  inline ``$...$`` math is left as passthrough for Obsidian.

pandoc is a *required* external binary for ``.tex`` input; pdflatex is required
only for display-math rendering (inline passthrough otherwise).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pymupdf as fitz

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

_TITLEPAGE_RE = re.compile(r"\\begin\{titlepage\}.*?\\end\{titlepage\}", re.DOTALL)

_IMAGE_RE = re.compile(r"!\[(?P<alt>.*?)\]\((?P<url>[^)]*)\)", re.DOTALL)

_DISPLAY_MATH_RE = re.compile(r"\$\$(?P<body>.*?)\$\$", re.DOTALL)

_BIB_CMD_RE = re.compile(r"\\(?:addbibresource|bibliography)\s*\{([^{}]+)\}")

_ATTR_RE = re.compile(r"(\]\([^)]*\))\{[^{}]*\}")

_CIT_ENTRY_RE = re.compile(
    r"^:{3,}\s*\{[^{}]*\.csl-entry[^{}]*\}\s*\n(.*?)\n:{3,}\s*$",
    re.DOTALL | re.MULTILINE,
)

_BIB_BODY_RE = re.compile(
    r"^:{3,}\s*\{[^{}]*\.csl-bib-body[^{}]*\}\s*$",
    re.MULTILINE,
)

_FENCE_RE = re.compile(r"^:{3,}\s*$", re.MULTILINE)


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


def _run_pandoc(
    body: str,
    project_dir: Path,
    bibs: list[Path],
    warnings: list[str],
) -> str:
    """Run pandoc on the flattened body; return the Markdown.

    Raises :class:`RuntimeError` when pandoc itself fails (timeout / non-zero
    exit), so the caller surfaces the pandoc message as the conversion error.
    """
    with tempfile.TemporaryDirectory(prefix="ptm-latex-") as tmp:
        tmpd = Path(tmp)
        flat = tmpd / "flattened.tex"
        flat.write_text(body, encoding="utf-8")
        out = tmpd / "out.md"
        cmd = [
            PANDOC_PATH,
            "-f",
            "latex",
            "-t",
            "markdown+tex_math_dollars+pipe_tables-simple_tables-multiline_tables-grid_tables",
            "--wrap=none",
            "--shift-heading-level-by=1",
            "--citeproc",
        ]
        for bib in bibs:
            cmd += ["--bibliography", str(bib)]
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
    """Strip pandoc inline attributes (``{#id}``, ``{key="v"}``) after links/images."""
    return _ATTR_RE.sub(r"\1", md)


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

            title = _strip_latex(_braced(flattened, "title") or "") or stem
            authors = _strip_latex(_braced(flattened, "author") or "")

            bibs = _find_bibs(flattened, project_dir)
            body = _strip_frontmatter(flattened)

            md = _run_pandoc(body, project_dir, bibs, result.warnings)
            md = _strip_attrs(md)
            md = _cleanup_bibliography(md)

            assets_dir = output_dir / "assets" / stem
            assets_dir.mkdir(parents=True, exist_ok=True)
            counter = [1]
            dedup: dict[str, str] = {}
            md = _rewrite_images(md, project_dir, assets_dir, stem, counter, dedup, result.warnings)
            md = _render_display_math(md, assets_dir, stem, counter, dedup, result.warnings)

            lines = [f"# {title}", ""]
            if authors:
                lines.extend([f"*{authors}*", ""])
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
