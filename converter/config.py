"""Runtime configuration for the optional AI passes (ADR-0012).

This module is the single source of truth for the *on/off* state of every AI
feature and for the local model servers those features talk to. Unlike the rest
of the converter, the enabled flags are **mutable at runtime**: launch flags
still set environment variables (``cli_common.apply_ai_env``), which seed the
state at import time, but the engine can flip the same state at runtime (the
TUI settings screen) — no restart required.

It also owns non-AI *output* preferences that share the same runtime-toggle
contract (``IMAGE_LINKS``, ADR-0047; ``PDF_COMPRESS``, ADR-0049;
``INFERENCE_OVERWRITE``, ADR-0050; ``PDF_BORDERLESS_TABLES``, ADR-0054;
``PDF_REFLOW``, ADR-0055; ``EPUB_OUTPUT``, ADR-0067): those
are read lazily at conversion time so an engine flip takes effect on the next
conversion, mirroring the lazy ``PDF_MODE`` read.

The AI modules consult :func:`is_enabled` at call time instead of reading a
module-level ``*_ENABLED`` constant, so a toggle takes effect on the next
conversion. Endpoint/model constants (``*_BASE_URL`` / ``*_MODEL`` /
``EMBED_*``) remain environment-read and stay reachable via ``--env``; this
module only *resolves* their effective base URLs (with the same documented
fallback chain) so the health probe can check the right endpoint.

Serving control goes through the **flaate control daemon** (ADR-0029): the
server catalog is a small built-in table whose entries carry role tags
(``vision``, ``transcriber``, ``summary``, …) and a fallback manifest key, and
endpoint resolution prefers the daemon's ``list`` projection over those static
defaults (``converter.fleet``, ADR-0064). The static defaults remain as the
offline fallback — never a second authority.
"""
from __future__ import annotations

import contextvars
import os
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable

from converter import fleet

_TRUE = {"1", "true", "yes", "on"}


def _env_true(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUE


# Context-local overrides for one conversion (ADR-0067). EPUB packaging needs
# the PDF converter's figure images even when the user keeps ``IMAGE_LINKS``
# off; page-render links retain the prior setting so an EPUB-only run does not
# write a PNG per page that pandoc would never embed. Scoped like the inference
# ledger so a batch thread cannot leak the override into the next file and the
# user's preference is never mutated.
_image_links_override: contextvars.ContextVar[bool | None] = contextvars.ContextVar(
    "ptm_image_links_override", default=None
)
_page_links_override: contextvars.ContextVar[bool | None] = contextvars.ContextVar(
    "ptm_page_links_override", default=None
)


@contextmanager
def force_epub_images() -> Iterator[None]:
    """Force figure extraction for one EPUB conversion (ADR-0067).

    Page-render links keep whatever the user's ``IMAGE_LINKS`` preference was
    before the block: an EPUB-only run (image links off) writes no page PNGs,
    while an explicit image-links run keeps them. The override is context-local
    and disappears when the block exits.
    """
    page_links = image_links_enabled()
    image_token = _image_links_override.set(True)
    page_token = _page_links_override.set(page_links)
    try:
        yield
    finally:
        _image_links_override.reset(image_token)
        _page_links_override.reset(page_token)


@dataclass(frozen=True)
class Server:
    """One local model server the AI passes depend on (ADR-0029, ADR-0064).

    ``tags`` is the role/capability match resolved against the control daemon's
    ``list`` projection; ``key`` is the fallback manifest key when the daemon is
    unreachable. The static ``host``/``port``/``runner``/``model`` fields are the
    offline fallback projection of the same entry. ``name`` is the stable
    internal role key; ``label`` is the user-facing capability name shown by the
    TUI (ADR-0065).
    """

    name: str
    key: str = ""
    tags: tuple[str, ...] = ()
    runner: str = ""
    host: str = "127.0.0.1"
    port: int = 0
    model: str = ""
    description: str = ""
    label: str = ""

    @property
    def model_key(self) -> str:
        """The manifest key the role currently resolves to (or the fallback)."""
        entry = fleet.find_by_tags(self.tags) if self.tags else None
        if entry:
            return str(entry.get("key") or entry.get("name") or self.key)
        return self.key or self.name

    @property
    def base_url(self) -> str:
        """The static fallback base URL (daemon-derived at runtime, ADR-0029)."""
        return f"http://{self.host}:{self.port}/v1"

    @property
    def start_command(self) -> str:
        """The on-demand start command via the control daemon's ``start`` verb."""
        return fleet.start_command(self.model_key)


@dataclass(frozen=True)
class Feature:
    """One togglable AI feature: its identity, display label, env var, and deps."""

    key: str
    label: str
    env_var: str
    description: str = ""
    implies: tuple[str, ...] = ()
    scope: str = "all"  # "all" | "paper" — paper features only run in paper mode


# Built-in catalog — the fallback projection of the fleet manifest's PtM-serving
# daemons (macos-dev-config/models.json, ADR-0029). The daemon list is preferred
# at runtime; these static rows only matter when the daemon is unreachable.
SERVERS: dict[str, Server] = {
    "transcriber": Server(
        name="transcriber",
        key="qwen2.5-vl-7b-instruct-4bit",
        tags=("vision", "transcriber"),
        runner="mlx-vlm",
        port=8081,
        model="mlx-community/Qwen2.5-VL-7B-Instruct-4bit",
        description="Vision and rewrite model (PresentationToMarkdown)",
        label="Vision + rewrite model",
    ),
    "classifier": Server(
        name="classifier",
        key="qwen2.5-vl-3b-instruct-4bit",
        tags=("vision", "classifier"),
        runner="mlx-vlm",
        port=8082,
        model="mlx-community/Qwen2.5-VL-3B-Instruct-4bit",
        description="Image classifier (PresentationToMarkdown)",
        label="Image classifier",
    ),
    "summary": Server(
        name="summary",
        # Resolves the manifest entry carrying text+summary — today the
        # Llama-3.2-3B (`llama-3.2-3b-instruct-4bit`, :8083). The manifest's
        # 1B summary model is a different role and appears later in manifest
        # order (flaate ADR-0007; ADR-0029).
        key="llama-3.2-3b-instruct-4bit",
        tags=("text", "summary"),
        runner="mlx-lm",
        port=8083,
        model="mlx-community/Llama-3.2-3B-Instruct-4bit",
        description="Summary chat model (PresentationToMarkdown)",
        label="Summary model",
    ),
    "structure-text": Server(
        name="structure-text",
        # Same model as summary, different role tag. The pre-migration :8085 was
        # a live port conflict with the old mistral daemon.
        key="llama-3.2-3b-instruct-4bit",
        tags=("text", "structure"),
        runner="mlx-lm",
        port=8083,
        model="mlx-community/Llama-3.2-3B-Instruct-4bit",
        description="Text model for summary + paper structure (PresentationToMarkdown)",
        label="Text model (summary + paper structure)",
    ),
    "embeddings": Server(
        name="embeddings",
        key="embeddinggemma-2-q8-0",
        tags=("embeddings",),
        runner="llama.cpp",
        port=8090,
        model="embeddinggemma-2-Q8_0.gguf",
        description="Embeddings model (PresentationToMarkdown)",
        label="Embeddings model",
    ),
}

FEATURES: dict[str, Feature] = {
    "vision": Feature(
        key="vision",
        label="Vision transcription",
        env_var="VISION_ENABLED",
        description=(
            "Reads images, diagrams and charts and turns them into Markdown "
            "text or a short description."
        ),
    ),
    "classify": Feature(
        key="classify",
        label="Image classifier",
        env_var="VISION_CLASSIFY_ENABLED",
        description=(
            "Decides which images are worth reading and skips photos, logos "
            "and backgrounds before vision runs. Turning it on also enables "
            "Vision transcription."
        ),
        implies=("vision",),
    ),
    "interpret": Feature(
        key="interpret",
        label="Diagram interpretation",
        env_var="INTERPRET_ENABLED",
        description=(
            "Reads the relationships a diagram asserts (for example, A "
            "supports B) and adds a plain-language explanation."
        ),
    ),
    "format": Feature(
        key="format",
        label="Markdown restructure",
        env_var="FORMAT_ENABLED",
        description=(
            "Rejoins wrapped lines and promotes heading-like bullets to "
            "headings; a word check rejects any change that drops content."
        ),
    ),
    "summary": Feature(
        key="summary",
        label="Presentation summary",
        env_var="SUMMARY_ENABLED",
        description=(
            "Adds a summary header with abstract, key topics, takeaways and "
            "key terms to each converted file."
        ),
    ),
    "structure": Feature(
        key="structure",
        label="Paper structure",
        env_var="STRUCTURE_ENABLED",
        description=(
            "Fixes titles, headings, column order and footnotes in paper-mode "
            "PDFs and rewords unreadable pages; no effect in slide mode."
        ),
        scope="paper",
    ),
}

# Mutable feature state, seeded from the environment at import time.
_state: dict[str, bool] = {}


def _init_state() -> None:
    for key, feature in FEATURES.items():
        _state[key] = _env_true(os.environ.get(feature.env_var))


_init_state()


def is_enabled(key: str) -> bool:
    """Whether an AI feature is currently on."""
    return bool(_state.get(key, False))


def set_enabled(key: str, value: bool) -> None:
    """Turn a feature on/off, propagating the ``implies`` relationship."""
    feature = FEATURES.get(key)
    if feature is None:
        raise KeyError(f"unknown feature: {key}")
    _state[key] = bool(value)
    if value:
        for dep in feature.implies:
            _state[dep] = True
    else:
        for other, other_feature in FEATURES.items():
            if key in other_feature.implies:
                _state[other] = False


def set_many(mapping: dict[str, bool]) -> None:
    """Apply several toggles at once."""
    for key, value in mapping.items():
        set_enabled(key, value)


def reset() -> None:
    """Re-read the environment (discarding any runtime overrides)."""
    _state.clear()
    _init_state()


def image_links_enabled() -> bool:
    """Whether asset image/page links are emitted into the Markdown.

    A non-AI output preference (ADR-0047), read lazily at conversion time like
    ``PDF_MODE``/``NEED_GATE`` so the engine can flip it per conversion
    without a restart. Default **off**: images are not extracted and no
    ``![...](...)``/``[Page N](...)`` links are written; AI descriptions are
    emitted in their place. ``IMAGE_LINKS=on`` restores the legacy behaviour.

    An EPUB export temporarily overrides this through
    :func:`force_epub_images` (ADR-0067), independently of the user setting.
    """
    override = _image_links_override.get()
    if override is not None:
        return override
    return _env_true(os.environ.get("IMAGE_LINKS"))


def page_links_enabled() -> bool:
    """Whether rendered page PNGs are saved and linked (ADR-0047).

    Defaults to :func:`image_links_enabled`. An EPUB export forces figure
    images on but leaves this at the user's prior setting (ADR-0067), so an
    EPUB-only run does not write a page render per page while an explicit
    image-links run keeps its page links.
    """
    override = _page_links_override.get()
    if override is not None:
        return override
    return image_links_enabled()


def epub_output_enabled() -> bool:
    """Whether a PDF conversion also writes an EPUB beside its Markdown.

    A lazy output preference like ``IMAGE_LINKS`` (ADR-0067), so the engine/TUI
    can toggle it per conversion. Default **off**; ``EPUB_OUTPUT=on`` enables
    the export. Applies to PDF inputs only.
    """
    return _env_true(os.environ.get("EPUB_OUTPUT"))


def epub_language() -> str:
    """Language tag for the EPUB metadata (ADR-0067).

    Empty means "let :mod:`converter.epub` derive it from the PDF catalog,
    falling back to ``en``"; ``EPUB_LANG`` overrides the derivation.
    """
    return os.environ.get("EPUB_LANG", "").strip()


def epub_cover_enabled() -> bool:
    """Whether the EPUB gets a cover rendered from the PDF's first page.

    Default **on**; ``EPUB_COVER=off`` keeps the export without a cover
    (ADR-0067). Best-effort: a page that cannot be rendered is simply skipped.
    """
    return (os.environ.get("EPUB_COVER", "on").strip().lower() or "on") in _TRUE


def pdf_compress_enabled() -> bool:
    """Whether archived PDFs are compressed after the archive move (ADR-0049).

    A non-AI output preference read lazily at archive time like ``IMAGE_LINKS``
    so the engine can flip it per conversion without a restart. Default
    **on**; ``PDF_COMPRESS=off`` disables compression. Compression itself is
    best-effort (see ``converter.compress``).
    """
    return (os.environ.get("PDF_COMPRESS", "on").strip().lower() or "on") in _TRUE


def inference_overwrite() -> bool:
    """Whether a re-run may replace an existing ``-inference-used.md`` (ADR-0050).

    A lazy output preference like ``IMAGE_LINKS``/``PDF_COMPRESS``, read at
    naming time. Default **off**: an existing marker-named file is never
    overwritten, the new conversion bumps to ``-inference-used-2`` instead.
    ``INFERENCE_OVERWRITE=on`` (``ptm --overwrite-inference``) opts into
    replacing it, for a deliberate re-run onto the same name.
    """
    return _env_true(os.environ.get("INFERENCE_OVERWRITE"))


def borderless_tables_enabled() -> bool:
    """Whether borderless (whitespace-only) PDF tables are reconstructed (ADR-0054).

    A non-AI output preference read lazily at conversion time like
    ``IMAGE_LINKS``/``PDF_COMPRESS``. Default **on**: pages whose tables carry
    no ruling lines (Word layout grids) get their tables rebuilt from the text
    layer and emitted as pipe tables; ``PDF_BORDERLESS_TABLES=off`` disables the
    detection and keeps the previous line-based output.
    """
    return (os.environ.get("PDF_BORDERLESS_TABLES", "on").strip().lower() or "on") in _TRUE


def pdf_reflow_enabled() -> bool:
    """Whether paper mode rejoins wrapped lines into paragraph text (ADR-0055).

    A non-AI output preference read lazily at conversion time like
    ``IMAGE_LINKS``/``PDF_COMPRESS``/``PDF_BORDERLESS_TABLES``. Default **on**:
    paper-mode PDFs emit one Markdown line per paragraph (and per section
    heading) built from the deterministic paragraph model, and paragraphs that
    span a column or page boundary are joined. ``PDF_REFLOW=off`` keeps the
    previous line-per-visual-line output (headings are still reconstructed);
    slide mode (``PDF_MODE=slide``) is never reflowed.
    """
    return (os.environ.get("PDF_REFLOW", "on").strip().lower() or "on") in _TRUE


def enabled_keys() -> list[str]:
    """The keys of currently enabled features, in registration order."""
    return [key for key in FEATURES if is_enabled(key)]


def enabled_features() -> list[Feature]:
    """The currently enabled features."""
    return [FEATURES[key] for key in enabled_keys()]


# Effective base-URL resolution. Each AI module keeps its own ``*_BASE_URL`` env
# read with the same defaults, but the probe needs a single place to resolve the
# endpoint a feature will actually hit, including the fallback chains
# (FORMAT -> WRITE, STRUCTURE -> FORMAT -> WRITE, SUMMARY -> WRITE).
#
# Chain: env override → control-daemon ``list`` (macos-dev-config manifest) →
# static catalog fallback (ADR-0029). The daemon lookup is cached by
# ``converter.fleet`` and never raises.
def _resolve(server_name: str, fallback_url: str) -> str:
    """Daemon-derived base URL for a role, else ``fallback_url``.

    Role tags win (flaate ADR-0007 / PtM ADR-0064); the static key is the
    one-release fallback for an old daemon manifest.
    """
    server = SERVERS[server_name]
    url = fleet.base_url_for_tags(server.tags) or fleet.base_url(server.key)
    return url or fallback_url


def _vision_url() -> str:
    return os.environ.get("VISION_BASE_URL", _resolve("transcriber", SERVERS["transcriber"].base_url))


def _classify_url() -> str:
    return os.environ.get("VISION_CLASSIFY_BASE_URL", _resolve("classifier", SERVERS["classifier"].base_url))


def _write_url() -> str:
    return os.environ.get("WRITE_BASE_URL", _resolve("transcriber", SERVERS["transcriber"].base_url))


def _format_url() -> str:
    return os.environ.get("FORMAT_BASE_URL", _write_url())


def _interpret_url() -> str:
    return os.environ.get("INTERPRET_BASE_URL", _write_url())


def _structure_url() -> str:
    return os.environ.get("STRUCTURE_BASE_URL", _format_url())


def _structure_text_url() -> str:
    return os.environ.get(
        "STRUCTURE_TEXT_BASE_URL", _resolve("structure-text", SERVERS["structure-text"].base_url)
    )


def _need_url() -> str:
    return os.environ.get(
        "NEED_BASE_URL", _resolve("structure-text", SERVERS["structure-text"].base_url)
    )


def _summary_url() -> str:
    return os.environ.get("SUMMARY_BASE_URL", _resolve("summary", SERVERS["summary"].base_url))


def _embed_url() -> str:
    return os.environ.get("EMBED_BASE_URL", _resolve("embeddings", SERVERS["embeddings"].base_url))


_FEATURE_ENDPOINTS: dict[str, list[tuple[str, Callable[[], str]]]] = {
    "vision": [("transcriber", _vision_url)],
    "classify": [("transcriber", _vision_url), ("classifier", _classify_url)],
    "interpret": [("transcriber", _interpret_url)],
    "format": [("transcriber", _format_url), ("structure-text", _need_url)],
    "summary": [("summary", _summary_url), ("embeddings", _embed_url)],
    "structure": [("transcriber", _structure_url), ("structure-text", _structure_text_url)],
}

# Roles whose endpoint is an optimisation or a graceful fallback rather than a
# hard requirement for the pass. The TUI marks these "optional" and does not
# block readiness on them.
_FEATURE_OPTIONAL: dict[str, frozenset[int]] = {
    "format": frozenset({1}),   # need-check gate; without it every slide is rewritten
    "summary": frozenset({1}),  # RAG embeddings; falls back to a bounded sample
}


def feature_endpoints(key: str) -> list[tuple[str, str]]:
    """Return ``(server_name, base_url)`` for the servers ``key`` needs."""
    endpoints = _FEATURE_ENDPOINTS[key]
    if key == "format" and (os.environ.get("NEED_GATE", "on").strip().lower() == "off"):
        # The need-check gate is disabled: the pass rewrites every slide on the
        # writer model and never touches the small text model.
        endpoints = endpoints[:1]
    return [(name, resolver()) for name, resolver in endpoints]


def _pass_models(key: str) -> list[str | None]:
    """Model ids the pass will request, aligned with ``feature_endpoints``.

    Deferred imports keep ``config`` free of the pass modules (the same idiom as
    :func:`_pass_model`); a partial import degrades to ``None`` rather than
    raising.
    """
    try:
        if key == "vision":
            from converter.vision import VISION_MODEL

            return [VISION_MODEL]
        if key == "classify":
            from converter.classify import VISION_CLASSIFY_MODEL
            from converter.vision import VISION_MODEL

            return [VISION_MODEL, VISION_CLASSIFY_MODEL]
        if key == "interpret":
            from converter.interpret import INTERPRET_MODEL

            return [INTERPRET_MODEL]
        if key == "format":
            from converter.format import FORMAT_MODEL
            from converter.need import NEED_MODEL

            return [FORMAT_MODEL, NEED_MODEL]
        if key == "summary":
            from converter.summary import EMBED_MODEL, SUMMARY_MODEL

            return [SUMMARY_MODEL, EMBED_MODEL]
        if key == "structure":
            from converter.structure import STRUCTURE_MODEL, STRUCTURE_TEXT_MODEL

            return [STRUCTURE_MODEL, STRUCTURE_TEXT_MODEL]
    except Exception:
        return []
    return []


def feature_requirements(key: str) -> list[dict]:
    """The models one pass needs, with the endpoint it will actually use.

    This is the projection the TUI renders (ADR-0065). ``model`` is the id the
    pass requests (the same constants the pass module uses); ``name``/``key``
    are the resolved fleet entry when the daemon is reachable, otherwise the
    static fallback. ``optional`` marks a graceful-degradation dependency (the
    need-check gate and summary embeddings).
    """
    models = _pass_models(key)
    optional = _FEATURE_OPTIONAL.get(key, frozenset())
    out: list[dict] = []
    for index, (server_name, base_url) in enumerate(feature_endpoints(key)):
        server = SERVERS[server_name]
        entry = fleet.find_by_tags(server.tags) if server.tags else None
        entry_key = str(entry.get("key") or entry.get("name") or "") if entry else ""
        key_name = entry_key or server.key or server.name
        name = str(entry.get("name") or "") if entry else ""
        out.append(
            {
                "role": server_name,
                "label": server.label or server.name,
                "key": key_name,
                "name": name or server.model or server.label or server.name,
                "base_url": base_url,
                "model": models[index] if index < len(models) else server.model,
                "start": fleet.start_command(key_name),
                "optional": index in optional,
            }
        )
    return out


def feature_info() -> list[dict]:
    """The user-facing capability list, in registration order (ADR-0065)."""
    return [
        {
            "key": feature.key,
            "label": feature.label,
            "env_var": feature.env_var,
            "description": feature.description,
            "implies": list(feature.implies),
            "scope": feature.scope,
        }
        for feature in FEATURES.values()
    ]


def probe(base_url: str, timeout: float = 1.5) -> bool:
    """Check whether an OpenAI-compatible server answers ``GET /models``.

    Never raises; ``False`` means "not reachable". Used only by the engine's
    model-health endpoint, never as part of a conversion.
    """
    url = base_url.rstrip("/") + "/models"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:  # noqa: BLE001 - any failure means "down"
        return False


def missing_servers(keys: list[str] | None = None) -> list[tuple[str, str, str]]:
    """Return ``(server_name, base_url, start_command)`` for down servers.

    Only servers required by the enabled features (deduped by base URL) are
    checked. Health comes from the control daemon's ``status`` verb first; an
    endpoint already up (started outside the daemon) is caught by the direct
    probe fallback. ``start_command`` is the daemon ``start`` curl (ADR-0029).
    """
    keys = keys if keys is not None else enabled_keys()
    checked: set[str] = set()
    missing: list[tuple[str, str, str]] = []
    for key in keys:
        optional = _FEATURE_OPTIONAL.get(key, frozenset())
        for index, (server_name, base_url) in enumerate(feature_endpoints(key)):
            if index in optional:
                # Graceful-degradation dependency: the pass runs without it.
                continue
            if base_url in checked:
                continue
            checked.add(base_url)
            if fleet.status_for_tags(SERVERS[server_name].tags) == "up":
                continue
            if fleet.status(SERVERS[server_name].model_key) == "up":
                continue
            if probe(base_url):
                continue
            missing.append(
                (server_name, base_url, SERVERS[server_name].start_command)
            )
    return missing


def _pass_model(key: str) -> str | None:
    """Resolve the model id a pass will use, via a deferred import (ADR-0016/0021).

    Deferred so ``config`` itself never imports the pass modules (avoids an
    import cycle) and so a missing/partial import degrades to ``None`` rather
    than raising.
    """
    try:
        if key == "vision":
            from converter.vision import VISION_MODEL

            return VISION_MODEL
        if key == "classify":
            from converter.classify import VISION_CLASSIFY_MODEL

            return VISION_CLASSIFY_MODEL
        if key == "interpret":
            from converter.interpret import INTERPRET_MODEL

            return INTERPRET_MODEL
        if key == "format":
            from converter.format import FORMAT_MODEL

            return FORMAT_MODEL
        if key == "structure":
            from converter.structure import STRUCTURE_MODEL

            return STRUCTURE_MODEL
        if key == "summary":
            from converter.summary import SUMMARY_MODEL

            return SUMMARY_MODEL
    except Exception:
        return None
    return None


def _embed_model() -> str | None:
    try:
        from converter.summary import EMBED_MODEL

        return EMBED_MODEL
    except Exception:
        return None


def _duplicate_if_exists() -> bool:
    """Read the persisted ``duplicate_if_exists`` preference (ADR-0015).

    Deferred import keeps ``config`` free of the settings/db layer at import
    time (the same idiom as ``_pass_model``/``_embed_model``).
    """
    try:
        from converter.settings import get_setting

        return get_setting("duplicate_if_exists", "off").strip().lower() in _TRUE
    except Exception:
        return False


def _vault_root() -> str | None:
    """Read the persisted ``vault_root`` preference.

    The Obsidian vault root the engine scans when an uploaded file's on-disk
    original cannot be resolved from ``recent_files`` (ADR-0028 fallback).
    """
    try:
        from converter.settings import get_setting

        value = get_setting("vault_root", "")
        return value or None
    except Exception:
        return None


def _audio_model() -> str:
    """The persisted ASR model as a canonical fleet name (ADR-0062).

    Settings written before the cutover hold mlx-whisper HF ids; normalizing on
    read keeps the TUI and run snapshots on fleet names without a migration.
    """
    value = _audio_setting("model", "nb-whisper-large")
    try:
        from converter.audio import normalize_audio_model

        return normalize_audio_model(value)
    except Exception:
        return value


def _audio_setting(key: str, default: str) -> str:
    """Read a persisted transcription setting (ADR-0043).

    Transcription preferences (model, language, diarization) live in the
    settings KV store; ``snapshot()`` exposes them to the TUI, and the engine
    applies them to each engine-hosted transcription job (ADR-0045) — they are
    import-time constants in ``converter``, so the engine sets the module state
    per job.
    """
    try:
        from converter.settings import get_setting

        value = get_setting("audio_" + key, "")
        return value if value else default
    except Exception:
        return default


def _audio_int(key: str, default: int) -> int:
    """Read a persisted numeric transcription setting, falling back to ``default``."""
    try:
        from converter.settings import get_setting

        value = get_setting("audio_" + key, "")
        return int(value) if value.strip() else default
    except Exception:
        return default


def _pandoc_available() -> bool:
    """Whether the external ``pandoc`` binary is available (ADR-0067)."""
    try:
        from converter.epub import pandoc_available

        return pandoc_available()
    except Exception:
        return False


def snapshot(probe: bool = True) -> dict:
    """Return a JSON-serialisable snapshot of the runtime AI configuration (ADR-0022).

    Captures ``PDF_MODE``, the on/off state of every feature, each enabled pass's
    resolved base URLs and model id, the embeddings model, the
    duplicate-if-exists preference, the image-link, PDF-compression,
    borderless-table, reflow, inference-overwrite and EPUB-output preferences,
    whether pandoc is available for EPUB packaging, and — when ``probe`` is
    true — the ``missing_servers()`` result. Never raises; used to persist a
    per-run record of "what models/toggles/servers were in effect".
    """
    passes: dict[str, dict] = {}
    for key in FEATURES:
        enabled = is_enabled(key)
        endpoints = (
            [{"server": name, "base_url": url} for name, url in feature_endpoints(key)]
            if enabled
            else []
        )
        passes[key] = {
            "enabled": enabled,
            "endpoints": endpoints,
            "model": _pass_model(key) if enabled else None,
        }
    try:
        missing = missing_servers() if probe else []
    except Exception:
        missing = []
    return {
        "pdf_mode": os.environ.get("PDF_MODE", "").strip().lower() or "slide",
        "image_links": image_links_enabled(),
        "pdf_compress": pdf_compress_enabled(),
        "borderless_tables": borderless_tables_enabled(),
        "pdf_reflow": pdf_reflow_enabled(),
        "inference_overwrite": inference_overwrite(),
        "epub_output": epub_output_enabled(),
        "pandoc": _pandoc_available(),
        "need_gate": os.environ.get("NEED_GATE", "on").strip().lower() or "on",
        "duplicate": _duplicate_if_exists(),
        "vault_root": _vault_root(),
        "audio_model": _audio_model(),
        "audio_language": _audio_setting("language", "no"),
        "audio_diarize": _audio_setting("diarize", "on").strip().lower() in _TRUE,
        "audio_speakers": _audio_int("speakers", 2),
        # Preprocessing is opt-in: dereverberation costs ~30 min per hour of
        # audio and DeepFilterNet has never worked in the audio venv, so both
        # default off and are one TUI toggle away (ADR-0069).
        "audio_dereverb": _audio_setting("dereverb", "off").strip().lower() in _TRUE,
        "audio_enhance": _audio_setting("enhance", "off").strip().lower() in _TRUE,
        "audio_prep_timeout": _audio_int("prep_timeout", 300),
        "audio_preflight": _audio_setting("preflight", "on").strip().lower() in _TRUE,
        "features": {key: is_enabled(key) for key in FEATURES},
        "feature_info": feature_info(),
        "passes": passes,
        "embed_model": _embed_model() if is_enabled("summary") else None,
        "missing_servers": missing,
    }
