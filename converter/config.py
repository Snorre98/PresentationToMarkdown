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

The server catalog is a small built-in table seeded from ``servers.conf``
(transcriber ``:8081``, classifier ``:8082``, ollama ``:11434``) and can be
refreshed from the sibling ``macos-dev-config/servers.conf`` when present.
"""
from __future__ import annotations

import os
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_TRUE = {"1", "true", "yes", "on"}


def _env_true(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUE


@dataclass(frozen=True)
class Server:
    """One local model server the AI passes depend on.

    ``name`` matches the ``serve.sh`` registry (``tools/serve.sh start <name>``).
    """

    name: str
    runner: str = ""
    host: str = "127.0.0.1"
    port: int = 0
    model: str = ""
    description: str = ""

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1"

    @property
    def serve_command(self) -> str:
        return f"tools/serve.sh start {self.name}"


@dataclass(frozen=True)
class Feature:
    """One togglable AI feature: its identity, GUI label, env var, and deps."""

    key: str
    label: str
    env_var: str
    description: str = ""
    implies: tuple[str, ...] = ()


# Built-in catalog — mirrors the rows in macos-dev-config/servers.conf that the
# AI passes actually reference. Refresh with refresh_servers_from_conf().
SERVERS: dict[str, Server] = {
    "transcriber": Server(
        name="transcriber",
        runner="mlx-vlm",
        port=8081,
        model="mlx-community/Qwen2.5-VL-7B-Instruct-4bit",
        description="Vision transcriber (PresentationToMarkdown)",
    ),
    "classifier": Server(
        name="classifier",
        runner="mlx-vlm",
        port=8082,
        model="mlx-community/Qwen2.5-VL-3B-Instruct-4bit",
        description="Classifier gate (PresentationToMarkdown)",
    ),
    "ollama": Server(
        name="ollama",
        runner="ollama",
        port=11434,
        description="Embeddings daemon (embeddinggemma / nomic-embed-text)",
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
# (FORMAT -> VISION, STRUCTURE -> FORMAT -> VISION).
def _vision_url() -> str:
    return os.environ.get("VISION_BASE_URL", SERVERS["transcriber"].base_url)


def _classify_url() -> str:
    return os.environ.get("VISION_CLASSIFY_BASE_URL", SERVERS["classifier"].base_url)


def _format_url() -> str:
    return os.environ.get("FORMAT_BASE_URL", _vision_url())


def _interpret_url() -> str:
    return os.environ.get("INTERPRET_BASE_URL", _vision_url())


def _structure_url() -> str:
    return os.environ.get("STRUCTURE_BASE_URL", _format_url())


def _summary_url() -> str:
    return os.environ.get("SUMMARY_BASE_URL", SERVERS["transcriber"].base_url)


def _embed_url() -> str:
    return os.environ.get("EMBED_BASE_URL", "http://localhost:11434/v1")


_FEATURE_ENDPOINTS: dict[str, list[tuple[str, Callable[[], str]]]] = {
    "vision": [("transcriber", _vision_url)],
    "classify": [("transcriber", _vision_url), ("classifier", _classify_url)],
    "interpret": [("transcriber", _interpret_url)],
    "format": [("transcriber", _format_url)],
    "summary": [("transcriber", _summary_url), ("ollama", _embed_url)],
    "structure": [("transcriber", _structure_url)],
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
    """Return ``(server_name, base_url, serve_command)`` for down servers.

    Only servers required by the enabled features (deduped by base URL) are
    checked. Pass ``keys`` to probe a specific subset instead.
    """
    keys = keys if keys is not None else enabled_keys()
    checked: set[str] = set()
    missing: list[tuple[str, str, str]] = []
    for key in keys:
        for server_name, base_url in feature_endpoints(key):
            if base_url in checked:
                continue
            checked.add(base_url)
            if not probe(base_url):
                missing.append(
                    (server_name, base_url, SERVERS[server_name].serve_command)
                )
    return missing


def _default_servers_conf() -> Path | None:
    """Locate the sibling macos-dev-config registry, or ``None`` if absent."""
    env = os.environ.get("PTM_SERVERS_CONF")
    if env:
        return Path(env)
    repo_root = Path(__file__).resolve().parents[1]
    candidate = repo_root.parent / "macos-dev-config" / "servers.conf"
    return candidate if candidate.exists() else None


def parse_servers_conf(text: str) -> dict[str, Server]:
    """Parse a ``serve.sh`` registry into ``{name: Server}``.

    Lines are ``name | runner | model | port | host | extra-args | description``;
    blank lines and ``#`` comments are ignored.
    """
    servers: dict[str, Server] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 5 or not parts[0]:
            continue
        name = parts[0]
        servers[name] = Server(
            name=name,
            runner=parts[1] if len(parts) > 1 else "",
            model=parts[2] if len(parts) > 2 else "",
            port=int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0,
            host=parts[4] if len(parts) > 4 and parts[4] else "127.0.0.1",
            description=parts[6] if len(parts) > 6 else "",
        )
    return servers


def refresh_servers_from_conf(path: str | Path | None = None) -> dict[str, Server] | None:
    """Merge a ``servers.conf`` registry into ``SERVERS``; return it or ``None``.

    Uses ``PTM_SERVERS_CONF`` or the sibling ``macos-dev-config`` path by
    default. Never raises.
    """
    resolved = Path(path) if path is not None else _default_servers_conf()
    if resolved is None:
        return None
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError:
        return None
    parsed = parse_servers_conf(text)
    SERVERS.update(parsed)
    return parsed
