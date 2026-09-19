"""Build a small, generic LaTeX project for the converter tests."""
from pathlib import Path

from tests.make_test_deck import make_png


def build_project(root: Path, name: str = "paper") -> Path:
    """Create a minimal LaTeX project under ``root`` and return its directory.

    Exercises the common constructs the converter handles: ``\\input`` includes,
    a ``\\title``/``\\author`` block, ``\\section`` headings, bold/italic,
    inline + display math, a figure with ``\\includegraphics`` + caption +
    label, a ``\\ref`` and a ``\\cite`` resolved via ``\\bibliography``.
    """
    project = root / name
    sections = project / "Sections"
    images = project / "images"
    sections.mkdir(parents=True)
    images.mkdir(parents=True)
    make_png(images / "plot.png")

    (project / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\usepackage{amsmath,graphicx}\n"
        "\\title{Test LaTeX Paper}\n"
        "\\author{Alice \\and Bob}\n"
        "\\date{2026}\n"
        "\\begin{document}\n"
        "\\maketitle\n"
        "\\input{Sections/intro}\n"
        "\\input{Sections/results}\n"
        "\\bibliographystyle{plain}\n"
        "\\bibliography{references}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    (project / "Sections" / "intro.tex").write_text(
        "\\section{Introduction}\n"
        "Here is some \\textbf{bold} text with \\emph{emphasis}.\n"
        "Inline math $x^2 + y^2 = z^2$.\n",
        encoding="utf-8",
    )
    (project / "Sections" / "results.tex").write_text(
        "\\section{Results}\n"
        "A display equation:\n"
        "\\begin{equation}\n"
        "E = mc^2\n"
        "\\end{equation}\n"
        "\\begin{figure}\n"
        "\\centering\n"
        "\\includegraphics[width=0.5\\textwidth]{images/plot.png}\n"
        "\\caption{A plot.}\n"
        "\\label{fig:plot}\n"
        "\\end{figure}\n"
        "See Figure \\ref{fig:plot} and \\cite{knuth1984}.\n",
        encoding="utf-8",
    )
    (project / "references.bib").write_text(
        "@book{knuth1984,\n"
        "  author = {Knuth, Donald E.},\n"
        "  title = {The TeXbook},\n"
        "  year = {1984},\n"
        "  publisher = {Addison-Wesley},\n"
        "}\n",
        encoding="utf-8",
    )
    # Build artifacts that project detection must ignore.
    (project / "main.aux").write_text("junk")
    (project / "main.log").write_text("junk")
    (project / "main.pdf").write_bytes(b"%PDF-junk")
    return project
