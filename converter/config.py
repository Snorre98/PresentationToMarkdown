"""Runtime configuration for the optional AI passes (ADR-0012).

This module is the single source of truth for the *on/off* state of every AI
feature and for the local model servers those features talk to. Unlike the rest
of the converter, the enabled flags are **mutable at runtime**: the CLI flags
still set environment variables (``cli_common.apply_ai_env``), which seed the
state at import time, but the GUI can now flip the same state with checkboxes —
no restart required.

The AI modules consult :func:`is_enabled` at call time instead of reading a
module-level ``*_ENABLED`` constant, so a toggle takes effect on the next
conversion. Endpoint/model constants (``*_BASE_URL`` / ``*_MODEL`` /
``EMBED_*``) remain environment-read and stay reachable via ``--env``; this
module only *resolves* their effective base URLs (with the same documented
fallback chain) so the health probe can check the right endpoint.

Serving control goes through the **macos-dev-config control daemon** (ADR-0029):
the server catalog is a small built-in table whose entries mirror the daemon's
fleet manifest (``models.json``), and endpoint resolution prefers the daemon's
``list`` projection over those static defaults (``converter.fleet``). The static
defaults remain as the offline fallback — never a second authority.
"""
from __future__ import annotations

import os
import urllib.request
from dataclasses import dataclass
from typing import Callable

from converter import fleet

_TRUE = {"1", "true", "yes", "on"}


def _env_true(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUE


@dataclass(frozen=True)
class Server:
    """One local model server the AI passes depend on (ADR-0029).

    ``daemon`` names the manifest daemon/model this feature is served by (the
    control daemon's fleet manifest, ``macos-dev-config/models.json``); the
    static ``host``/``port``/``runner``/``model`` fields are the offline
    fallback projection of the same entry, used only when the daemon is
    unreachable. ``name`` is the feature label, stable across the migration.
    """

    name: str
    daemon: str = ""
    runner: str = ""
    host: str = "127.0.0.1"
    port: int = 0
    model: str = ""
    description: str = ""

    @property
    def daemon_name(self) -> str:
        """The manifest name start/status/release target (defaults to ``name``)."""
        return self.daemon or self.name

    @property
    def base_url(self) -> str:
        """The static fallback base URL (daemon-derived at runtime, ADR-0029)."""
        return f"http://{self.host}:{self.port}/v1"

    @property
    def start_command(self) -> str:
        """The on-demand start command via the control daemon's ``start`` verb."""
        return fleet.start_command(self.daemon_name)


@dataclass(frozen=True)
class Feature:
    """One togglable AI feature: its identity, GUI label, env var, and deps."""

    key: str
    label: str
    env_var: str
    description: str = ""
    implies: tuple[str, ...] = ()


# Built-in catalog — the fallback projection of the fleet manifest's PtM-serving
# daemons (macos-dev-config/models.json, ADR-0029). The daemon list is preferred
# at runtime; these static rows only matter when the daemon is unreachable.
SERVERS: dict[str, Server] = {
    "transcriber": Server(
        name="transcriber",
        daemon="transcriber",
        runner="mlx-vlm",
        port=8081,
        model="mlx-community/Qwen2.5-VL-7B-Instruct-4bit",
        description="Vision transcriber (PresentationToMarkdown)",
    ),
    "classifier": Server(
        name="classifier",
        daemon="classifier",
        runner="mlx-vlm",
        port=8082,
        model="mlx-community/Qwen2.5-VL-3B-Instruct-4bit",
        description="Classifier gate (PresentationToMarkdown)",
    ),
    "summary": Server(
        name="summary",
        # Served by the manifest's `text` daemon (Llama-3.2-3B, :8083) — the same
        # model PtM's summary pass always used; the manifest's own `summary`
        # daemon (1B) belongs to the writing-assistant engine (ADR-0029).
        daemon="text",
        runner="mlx-lm",
        port=8083,
        model="mlx-community/Llama-3.2-3B-Instruct-4bit",
        description="Summary chat (PresentationToMarkdown)",
    ),
    "structure-text": Server(
        name="structure-text",
        # Also served by the manifest's `text` daemon (:8083) — the pre-migration
        # :8085 was a live port conflict with the manifest's mistral-24b daemon.
        daemon="text",
        runner="mlx-lm",
        port=8083,
        model="mlx-community/Llama-3.2-3B-Instruct-4bit",
        description="Small text model for the structure pass text regime (PresentationToMarkdown)",
    ),
    "nomic-embed": Server(
        name="nomic-embed",
        daemon="nomic-embed",
        runner="llama.cpp",
        port=8090,
        model="nomic-embed-text",
        description="Embeddings daemon (nomic-embed-text GGUF)",
    ),
}

FEATURES: dict[str, Feature] = {
    "vision": Feature(
        key="vision",
        label="Vision transcription",
        env_var="VISION_ENABLED",
        description="Transcribe diagrams, flowcharts and tables that appear as images",
    ),
    "classify": Feature(
        key="classify",
        label="Classifier gate",
        env_var="VISION_CLASSIFY_ENABLED",
        description="Cheap VLM gate that skips decorative images (implies vision)",
        implies=("vision",),
    ),
    "interpret": Feature(
        key="interpret",
        label="Diagram interpretation",
        env_var="INTERPRET_ENABLED",
        description="Extract typed relationships and meaning from diagrams",
    ),
    "format": Feature(
        key="format",
        label="LLM restructure",
        env_var="FORMAT_ENABLED",
        description="Reflow wrapped lines and promote heading-like bullets",
    ),
    "summary": Feature(
        key="summary",
        label="RAG summary",
        env_var="SUMMARY_ENABLED",
        description="Prepend a standardized per-presentation summary header",
    ),
    "structure": Feature(
        key="structure",
        label="Paper structure",
        env_var="STRUCTURE_ENABLED",
        description="LLM document-structure pass (paper-mode PDFs only)",
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
    """Daemon-derived base URL for ``server_name``, else ``fallback_url``."""
    url = fleet.base_url(SERVERS[server_name].daemon_name)
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


def _summary_url() -> str:
    return os.environ.get("SUMMARY_BASE_URL", _resolve("summary", SERVERS["summary"].base_url))


def _embed_url() -> str:
    return os.environ.get("EMBED_BASE_URL", _resolve("nomic-embed", SERVERS["nomic-embed"].base_url))


_FEATURE_ENDPOINTS: dict[str, list[tuple[str, Callable[[], str]]]] = {
    "vision": [("transcriber", _vision_url)],
    "classify": [("transcriber", _vision_url), ("classifier", _classify_url)],
    "interpret": [("transcriber", _interpret_url)],
    "format": [("transcriber", _format_url)],
    "summary": [("summary", _summary_url), ("nomic-embed", _embed_url)],
    "structure": [("transcriber", _structure_url), ("structure-text", _structure_text_url)],
}


def feature_endpoints(key: str) -> list[tuple[str, str]]:
    """Return ``(server_name, base_url)`` for the servers ``key`` needs."""
    return [(name, resolver()) for name, resolver in _FEATURE_ENDPOINTS[key]]


def probe(base_url: str, timeout: float = 1.5) -> bool:
    """Check whether an OpenAI-compatible server answers ``GET /models``.

    Never raises; ``False`` means "not reachable". Used only by the GUI's health
    check (and available to the CLI), never as part of a conversion.
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
        for server_name, base_url in feature_endpoints(key):
            if base_url in checked:
                continue
            checked.add(base_url)
            if fleet.status(SERVERS[server_name].daemon_name) == "up":
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
    """Read the persisted ``duplicate_if_exists`` preference (ADR-0028/GUI parity).

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


def snapshot(probe: bool = True) -> dict:
    """Return a JSON-serialisable snapshot of the runtime AI configuration (ADR-0022).

    Captures ``PDF_MODE``, the on/off state of every feature, each enabled pass's
    resolved base URLs and model id, the embeddings model, the duplicate-if-exists
    preference, and — when ``probe`` is true — the ``missing_servers()`` result.
    Never raises; used to persist a per-run record of "what models/toggles/servers
    were in effect".
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
        "duplicate": _duplicate_if_exists(),
        "vault_root": _vault_root(),
        "features": {key: is_enabled(key) for key in FEATURES},
        "passes": passes,
        "embed_model": _embed_model() if is_enabled("summary") else None,
        "missing_servers": missing,
    }
