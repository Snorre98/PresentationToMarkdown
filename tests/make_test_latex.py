"""Build small, generic LaTeX projects for the converter tests."""
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
        "\\begin{table}[H]\n"
        "\\centering\n"
        "\\caption{Escaped literals}\n"
        "\\begin{tabular}{ll}\\hline\n"
        "\\textbf{Item} & \\textbf{Note} \\\\ \\hline\n"
        "Failures & 7\\% (\\textasciitilde{}18/yr) \\\\ \\hline\n"
        "Cost & [TBD] \\\\ \\hline\n"
        "\\end{tabular}\n"
        "\\end{table}\n"
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


def build_titlepage_project(root: Path, name: str = "thesis") -> Path:
    """Create a project whose title lives on a title page, not in ``\\title``.

    Mirrors the NTNU report template: ``title.tex`` is ``\\import``-ed into the
    document, carries a logo, two small-caps course lines, the real title in
    ``{\\huge \\bfseries …}``, a numbered members table, a conditional word
    count (``\\IfFileExists`` + ``\\input``, as ``texcount`` writes it) and a
    term line. There is no ``\\title``/``\\author``, so the title must be
    derived and the rest of the front block preserved (ADR-0056/0058/0059).
    The body's own table then numbers as Table 2 and both tables are referenced.
    """
    project = root / name
    sections = project / "Sections"
    images = project / "images"
    sections.mkdir(parents=True)
    images.mkdir(parents=True)
    make_png(images / "logo.png")

    (project / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\usepackage{graphicx,import}\n"
        "\\begin{document}\n"
        "\\import{./}{title}\n"
        "\\tableofcontents\n"
        "\\input{Sections/intro}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    (project / "title.tex").write_text(
        "\\begin{titlepage}\n"
        "\\begin{center}\n"
        "\\includegraphics[width=0.40\\textwidth]{images/logo.png}\\\\[1cm]\n"
        "\\textsc{\\LARGE Department of Computer Science}\\\\[1.5cm]\n"
        "\\textsc{\\Large TDT4259 Applied Data Science}\\\\[0.5cm]\n"
        "{\\huge \\bfseries Assignment 7 - Problem D}\\\\[0.4cm]\n"
        "\\begin{table}[H]\n"
        "\\centering\n"
        "\\caption{Members}\n"
        "\\label{tab:members}\n"
        "\\begin{tabular}{|p{3cm}|p{7cm}|}\\hline\n"
        "\\textbf{Given name} & \\textbf{Familyname} \\\\ \\hline\n"
        "Alice & Smith \\\\ \\hline\n"
        "\\end{tabular}\n"
        "\\end{table}\n"
        "\\vfill\n"
        "{\\large Word count: \\IfFileExists{wordcount.tex}{\\input{wordcount.tex} words}{N/A}}\\\\[0.5cm]\n"
        "{\\large Autumn 2026}\n"
        "\\end{center}\n"
        "\\end{titlepage}\n",
        encoding="utf-8",
    )
    (project / "wordcount.tex").write_text("1234\n", encoding="utf-8")
    (project / "Sections" / "intro.tex").write_text(
        "\\section{Introduction}\n"
        "A short introduction.\n"
        "\\begin{table}[H]\n"
        "\\centering\n"
        "\\caption{Key figures}\n"
        "\\label{tab:kpi}\n"
        "\\begin{tabular}{ll}\\hline\n"
        "\\textbf{KPI} & \\textbf{Value} \\\\ \\hline\n"
        "Net benefit & 840k \\\\ \\hline\n"
        "\\end{tabular}\n"
        "\\end{table}\n"
        "See \\autoref{tab:kpi} and \\autoref{tab:members}.\n",
        encoding="utf-8",
    )
    return project


def build_figure_project(root: Path, name: str = "figs") -> Path:
    """Create a project exercising the figure and cross-reference passes.

    ADR-0056/0057: a TikZ flowchart that leans on a project-local ``\\tikzset``
    style (``packages.sty``), a pgfplots axis, an ``\\includegraphics`` figure,
    a caption with ``$…$`` math, a numbered table caption, and
    ``\\autoref``/``\\ref``/``\\eqref`` references (including one to a label
    that does not exist).
    """
    project = root / name
    images = project / "images"
    images.mkdir(parents=True)
    make_png(images / "plot.png")
    (project / "packages.sty").write_text(
        "\\usepackage{tikz}\n"
        "\\usetikzlibrary{arrows.meta, positioning}\n"
        "\\tikzset{box/.style = {rectangle, draw, fill=blue!20, align=center}}\n",
        encoding="utf-8",
    )
    (project / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\usepackage{packages}\n"
        "\\usepackage{pgfplots}\n"
        "\\usepackage{graphicx}\n"
        "\\title{Figures}\n"
        "\\author{Alice}\n"
        "\\begin{document}\n"
        "\\maketitle\n"
        "\\section{Shapes}\n"
        "\\label{sec:shapes}\n"
        "A flow chart:\n"
        "\\begin{figure}[H]\n"
        "\\setlength{\\abovecaptionskip}{2pt}\n"
        "\\centering\n"
        "\\begin{tikzpicture}\n"
        "\\node [box] (a) at (0,0) {start};\n"
        "\\node [box, right=of a] (b) {finish\\\\with $c_{FN} \\gg c_{FP}$};\n"
        "\\draw [-{Latex}] (a) -- (b) node[midway, above] {above the line};\n"
        "\\end{tikzpicture}\n"
        "\\caption{The $c_{FN} \\gg c_{FP}$ flow.}\n"
        "\\label{fig:flow}\n"
        "\\end{figure}\n"
        "\\begin{figure}[H]\n"
        "\\centering\n"
        "\\begin{tikzpicture}\n"
        "\\begin{axis}[width=0.6\\linewidth, height=4cm]\n"
        "\\addplot[blue, thick, domain=0:1, samples=2] {x};\n"
        "\\end{axis}\n"
        "\\end{tikzpicture}\n"
        "\\caption{A pgfplots line.}\n"
        "\\label{fig:chart}\n"
        "\\end{figure}\n"
        "\\begin{figure}[H]\n"
        "\\centering\n"
        "\\includegraphics[width=0.4\\textwidth]{images/plot.png}\n"
        "\\caption{A picture.}\n"
        "\\label{fig:pic}\n"
        "\\end{figure}\n"
        "\\section{References}\n"
        "\\label{sec:refs}\n"
        "See \\autoref{fig:flow}, \\ref{fig:chart}, \\autoref{tab:kpi}, "
        "\\eqref{eq:main} and \\autoref{app:missing}.\n"
        "\\begin{equation}\n"
        "\\label{eq:main}\n"
        "E = mc^2\n"
        "\\end{equation}\n"
        "\\begin{table}[H]\n"
        "\\centering\n"
        "\\caption{Key figures}\n"
        "\\label{tab:kpi}\n"
        "\\begin{tabular}{ll}\\hline\n"
        "\\textbf{KPI} & \\textbf{Value} \\\\ \\hline\n"
        "Net benefit & 840k \\\\ \\hline\n"
        "\\end{tabular}\n"
        "\\end{table}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    return project


def build_bibliography_project(root: Path, name: str = "bib") -> Path:
    """Create a project exercising citation and bibliography fidelity.

    ADR-0061: an ``@inproceedings`` with a full page range and a title-cased
    ``booktitle``, a ``@book`` with a publisher place, and two same-author,
    same-year ``@online`` entries (which must disambiguate to ``2025a``/
    ``2025b`` and collapse to an em dash in the bibliography).
    """
    project = root / name
    project.mkdir(parents=True)
    (project / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "\\section{References}\n"
        "See \\parencite{elkan_2001}, \\parencite{hernan_2020} "
        "and \\parencite{ibm_a,ibm_b}.\n"
        "\\printbibliography\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    (project / "references.bib").write_text(
        "@inproceedings{elkan_2001,\n"
        "  author = {Elkan, Charles},\n"
        "  title = {The Foundations of Cost-Sensitive Learning},\n"
        "  booktitle = {Proceedings of the Seventeenth International Joint "
        "Conference on Artificial Intelligence (IJCAI)},\n"
        "  pages = {973--978},\n"
        "  year = {2001},\n"
        "  url = {https://example.org/elkan.pdf},\n"
        "}\n"
        "\n"
        "@book{hernan_2020,\n"
        "  author = {Hern{\\'a}n, Miguel A. and Robins, James M.},\n"
        "  title = {Causal Inference: What If},\n"
        "  publisher = {Chapman \\& Hall/CRC},\n"
        "  address = {Boca Raton},\n"
        "  year = {2020},\n"
        "}\n"
        "\n"
        "@online{ibm_a,\n"
        "  author = {IBM},\n"
        "  title = {Causal Inference},\n"
        "  year = {2025},\n"
        "  url = {https://example.org/causal},\n"
        "  urldate = {2026-09-30},\n"
        "}\n"
        "\n"
        "@online{ibm_b,\n"
        "  author = {IBM},\n"
        "  title = {What is human-in-the-loop?},\n"
        "  year = {2025},\n"
        "  url = {https://example.org/hitl},\n"
        "  urldate = {2026-09-30},\n"
        "}\n",
        encoding="utf-8",
    )
    return project
