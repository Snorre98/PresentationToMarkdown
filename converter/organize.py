"""Default output layout: Markdown beside the source, sources archived (ADR-0048).

By default (no explicit output folder) a conversion writes its ``.md`` **next
to the source file** — using a kebab-case name normalized from it — and then
moves the converted source into a sibling ``source/`` archive folder with the
same normalized name::

    foo/03 What makes things fun.pdf
      -> foo/03-what-makes-things-fun.md
      -> foo/source/03-what-makes-things-fun.pdf

Files that already live in a ``source/`` folder are treated as archived: their
``.md`` is written into the ``source`` folder's parent and nothing is moved, so
re-runs are idempotent. An explicit output folder disables organizing entirely
(no rename, no move); see ADR-0048.

The move never overwrites silently within a batch: two different sources whose
names normalize identically get a ``-2`` suffix plus a warning. Across runs an
existing archive copy is replaced (deterministic names), using a
move-aside/restore dance so a failed move cannot destroy the previous copy.

Markdown that contains model-produced content is written as
``<stem>-inference-used.md`` (ADR-0050). The marker is appended *after* the
conversion — only then is it known whether any AI pass contributed — and an
existing marked file is never overwritten: the rename bumps to
``-inference-used-2``, ``-3``, … unless the ``INFERENCE_OVERWRITE`` preference
explicitly asks for a replacement.
"""
from __future__ import annotations

import os
import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

SOURCE_DIR_NAME = "source"
_FALLBACK_STEM = "document"
_MAX_STEM_LEN = 100

# Suffix marking a Markdown file that contains model-produced content (ADR-0050).
# Appended after the kebab-case truncation, so the marker itself is never cut.
INFERENCE_MARKER = "-inference-used"
_MARKED_RE = re.compile(rf"{re.escape(INFERENCE_MARKER)}(?:-\d+)?$")

# Characters that NFKD does not decompose into an ASCII base letter. Applied
# after lowercasing; the rest of the Latin accents fall out of the NFKD strip.
_TRANSLITERATE = str.maketrans(
    {
        "ø": "o",
        "æ": "ae",
        "ß": "ss",
        "đ": "d",
        "ð": "d",
        "þ": "th",
        "ł": "l",
        "œ": "oe",
        "ı": "i",
        "ſ": "s",
    }
)

OutputDirArg = str | Path | Callable[[Path], "Path | None"] | None
SourceResolver = Callable[[Path], "Path | None"]


def kebab_stem(name: str, *, max_len: int = _MAX_STEM_LEN) -> str:
    """Normalize ``name`` to a lowercase kebab-case stem (ADR-0048).

    Accents are folded to ASCII (``ø→o``, ``æ→ae``, ``é→e``, …); every run of
    non-alphanumeric characters becomes a single ``-``; leading digits are kept
    so ordered lecture names stay ordered. Non-Latin letters are preserved.
    Names longer than ``max_len`` are cut at a hyphen boundary; a name with no
    usable characters becomes ``document``.
    """
    text = unicodedata.normalize(
        "NFKD", name.strip().lower().translate(_TRANSLITERATE)
    )
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    parts: list[str] = []
    current: list[str] = []
    for ch in text:
        if ch.isalnum():
            current.append(ch)
        elif current:
            parts.append("".join(current))
            current = []
    if current:
        parts.append("".join(current))
    stem = "-".join(parts)
    if len(stem) > max_len:
        cut = stem[:max_len]
        boundary = cut.rfind("-")
        if boundary >= max_len // 2:
            cut = cut[:boundary]
        stem = cut.rstrip("-")
    return stem or _FALLBACK_STEM


def is_archived(path: Path) -> bool:
    """Whether ``path`` already sits directly inside a ``source/`` archive."""
    return path.parent.name.lower() == SOURCE_DIR_NAME


def default_output_dir(path: Path) -> Path:
    """The default output folder for ``path`` (ADR-0048).

    The source's own folder; for a file already inside a ``source/`` archive,
    the archive folder's parent (where its Markdown belongs).
    """
    parent = path.parent
    if parent.name.lower() == SOURCE_DIR_NAME:
        return parent.parent
    return parent


def is_marked_stem(stem: str) -> bool:
    """Whether ``stem`` already carries the inference marker (ADR-0050)."""
    return bool(_MARKED_RE.search(stem))


def marked_stem(stem: str) -> str:
    """Return ``stem`` with the inference marker appended.

    An already-marked stem (including a bumped ``…-inference-used-2``) is
    returned unchanged, so a conversion can never double-mark a name.
    """
    return stem if is_marked_stem(stem) else f"{stem}{INFERENCE_MARKER}"


def _exists(output_dir: Path, stem: str) -> bool:
    """Whether ``stem`` is taken, as Markdown, EPUB or an assets folder.

    The EPUB is a second artifact of the same conversion (ADR-0067), so the
    collision/marker checks must see it too: a run must not overwrite a marked
    EPUB just because its Markdown was moved or deleted.
    """
    return (
        (output_dir / f"{stem}.md").exists()
        or (output_dir / f"{stem}.epub").exists()
        or (output_dir / "assets" / stem).exists()
    )


def claim_inference_path(
    output_dir: Path, stem: str, warnings: list[str], *, overwrite: bool = False
) -> Path:
    """Return the final ``.md`` path for inference-produced content (ADR-0050).

    Prefers ``<stem>-inference-used.md``; when that already exists the next free
    ``-inference-used-2``/``-3``/… is used instead (with a warning), because a
    file that cost model time is never replaced by accident. ``overwrite`` is
    the explicit ``--overwrite-inference`` / ``INFERENCE_OVERWRITE`` opt-out:
    it replaces the marked file rather than bumping.
    """
    marked = marked_stem(stem)
    target = output_dir / f"{marked}.md"
    if overwrite or not _exists(output_dir, marked):
        return target
    n = 2
    while _exists(output_dir, f"{marked}-{n}"):
        n += 1
    bumped = f"{marked}-{n}.md"
    warnings.append(
        f"Inference output '{target.name}' already exists; wrote '{bumped}' "
        f"instead (use --overwrite-inference to replace it)"
    )
    return output_dir / bumped


def avoid_marked_artifact(output_dir: Path, stem: str, warnings: list[str]) -> str:
    """Keep a plain conversion from clobbering a marker-named artifact.

    A source literally named like a marked file (``deck-inference-used.pdf``)
    would otherwise write its plain ``deck-inference-used.md`` straight over the
    artifact an earlier inference run produced. When that target exists the
    next free ``-2``/``-3`` stem is used (with a warning) instead. Returns the
    stem to convert with, unchanged in every other case.
    """
    if not (is_marked_stem(stem) and _exists(output_dir, stem)):
        return stem
    n = 2
    while _exists(output_dir, f"{stem}-{n}"):
        n += 1
    bumped = f"{stem}-{n}"
    warnings.append(
        f"Inference artifact '{stem}.md' already exists; using '{bumped}.md'"
    )
    return bumped


def mark_inference_output(
    md_path: Path, stem: str, warnings: list[str], *, overwrite: bool = False
) -> Path:
    """Rename a freshly written ``.md`` to its marker-named counterpart (ADR-0050).

    Returns the final path: ``md_path`` itself when it already carries the marker,
    the renamed path otherwise. Best-effort — a failed rename only warns and
    leaves the unmarked file in place, because naming never fails a conversion.
    """
    try:
        target = claim_inference_path(
            md_path.parent, stem, warnings, overwrite=overwrite
        )
        if target == md_path:
            return md_path
        _remove(target)
        os.replace(md_path, target)
        return target
    except OSError as exc:
        warnings.append(f"Could not mark inference output: {exc}")
        return md_path


@dataclass
class Layout:
    """Where one conversion's Markdown goes and where its source is archived."""

    output_dir: Path
    stem: str
    source_dest: Path | None = None
    warnings: list[str] = field(default_factory=list)


class LayoutResolver:
    """Decide the layout for every input of one conversion batch (ADR-0048).

    A resolver instance belongs to one ``convert_files`` batch and remembers
    the normalized names it has claimed, so two *different* sources that
    normalize to the same name cannot silently overwrite each other: the second
    one becomes ``<stem>-2`` (Markdown and archive copy alike) with a warning.
    """

    def __init__(
        self,
        output_dir: OutputDirArg = None,
        *,
        organize: bool | None = None,
        source_resolver: SourceResolver | None = None,
    ) -> None:
        self._output_dir = output_dir
        self._organize = organize
        self._source_resolver = source_resolver
        self._claimed_md: dict[Path, set[str]] = {}
        self._claimed_dests: set[Path] = set()

    def resolve(self, path: Path) -> Layout:
        """Return the :class:`Layout` for one source ``path``."""
        if self._output_dir is not None and not callable(self._output_dir):
            # Explicit output folder: no renaming, no move, no compression.
            return Layout(Path(self._output_dir), path.stem)

        organize = self._organize
        if organize is None:
            # Default: organize, except when a per-file output *resolver*
            # deliberately picked a folder and no source_resolver says the
            # conversion represents another on-disk file (the engine/upload
            # case, ADR-0028).
            organize = self._source_resolver is not None or not callable(
                self._output_dir
            )
        layout_source = path
        if organize and self._source_resolver is not None:
            resolved_source = self._source_resolver(path)
            if resolved_source is None:
                organize = False
            else:
                layout_source = Path(resolved_source)

        explicit_dir = self._resolve_output(path)
        if not organize:
            return Layout(explicit_dir or default_output_dir(path), path.stem)

        resolved_dir = explicit_dir or default_output_dir(layout_source)
        name = layout_source.name if layout_source.is_dir() else layout_source.stem
        warnings: list[str] = []
        stem = self._claim_md_stem(resolved_dir, kebab_stem(name), warnings)
        dest = None
        if not is_archived(layout_source):
            dest = self._claim_source_dest(layout_source, stem, warnings)
        return Layout(resolved_dir, stem, dest, warnings)

    def _resolve_output(self, path: Path) -> Path | None:
        if callable(self._output_dir):
            result = self._output_dir(path)
            return Path(result) if result else None
        if self._output_dir is not None:
            return Path(self._output_dir)
        return None

    def _claim_md_stem(self, output_dir: Path, stem: str, warnings: list[str]) -> str:
        claimed = self._claimed_md.setdefault(output_dir, set())
        candidate, n = stem, 2
        while candidate in claimed:
            candidate = f"{stem}-{n}"
            n += 1
        claimed.add(candidate)
        if candidate != stem:
            warnings.append(
                f"Name collision: '{stem}' is already used by another converted "
                f"file; using '{candidate}'"
            )
        return candidate

    def _claim_source_dest(
        self, source: Path, stem: str, warnings: list[str]
    ) -> Path | None:
        archive_dir = source.parent / SOURCE_DIR_NAME
        if archive_dir == source or source in archive_dir.parents:
            # A project folder literally named like the archive cannot archive
            # into itself; leave it in place.
            warnings.append(
                f"'{source.name}' is named like the archive folder; left in place"
            )
            return None
        suffix = "" if source.is_dir() else source.suffix.lower()
        plain = f"{stem}{suffix}"
        candidate, n = plain, 2
        while archive_dir / candidate in self._claimed_dests:
            candidate = f"{stem}-{n}{suffix}"
            n += 1
        dest = archive_dir / candidate
        self._claimed_dests.add(dest)
        if candidate != plain:
            warnings.append(
                f"Name collision: archived as '{candidate}' instead of '{plain}'"
            )
        return dest


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def archive_source(source: Path, dest: Path, warnings: list[str]) -> Path | None:
    """Move ``source`` to ``dest``, replacing an existing archive copy.

    The existing destination (if any) is moved aside first and restored when
    the move fails, so a failed archive attempt never destroys the previous
    copy. Returns the archive path, or ``None`` on failure (with a warning).
    """
    backup: Path | None = None
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() or dest.is_symlink():
            backup = dest.with_name(f".{dest.name}.ptm-backup")
            _remove(backup)
            os.replace(dest, backup)
        shutil.move(str(source), str(dest))
    except OSError as exc:
        if backup is not None:
            try:
                os.replace(backup, dest)
            except OSError:
                pass
        warnings.append(f"Could not archive source: {exc}")
        return None
    if backup is not None:
        _remove(backup)
    return dest
