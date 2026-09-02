"""Best-effort model-residency orchestration across the local model servers (ADR-0017).

This 32 GB Mac has no VRAM cap and no OOM killer, so the app must actively
unload models between passes instead of relying on the OS to swap. Two of the
runners expose a programmatic unload:

- **mlx-vlm** — ``POST <root>/unload`` (never auto-unloads, so a 7B VLM persists
  across runs unless released).
- **Ollama** — ``POST <root>/api/generate`` with ``{"model": ..., "keep_alive": 0}``
  (the native API at the host root, NOT under ``/v1``).

Everything here is best-effort and never raises: a failed unload only means the
model lingers in memory, which is the pre-existing behaviour. The module has no
UI dependency and requires no running server.
"""
from __future__ import annotations

import json
import urllib.request
from urllib.parse import urlparse

from converter import config

_TIMEOUT = 5.0


def _root(base_url: str) -> str:
    """Return the server root (base URL minus a trailing ``/v1``)."""
    url = base_url.rstrip("/")
    if url.endswith("/v1"):
        url = url[: -len("/v1")]
    return url


def _normalize_host(host: str) -> str:
    """Fold ``localhost`` and ``127.0.0.1`` into one key for host:port matching."""
    h = (host or "").lower().rstrip(".")
    return "127.0.0.1" if h in ("localhost", "127.0.0.1") else h


def _host_port(base_url: str) -> tuple[str, int] | None:
    try:
        parsed = urlparse(base_url)
        return _normalize_host(parsed.hostname or ""), parsed.port or 0
    except Exception:  # noqa: BLE001 - a malformed URL just means "unresolvable"
        return None


def resolve_runner(base_url: str) -> str | None:
    """Map an effective base URL to a ``config.SERVERS`` runner by host:port.

    Normalizes ``localhost`` ↔ ``127.0.0.1`` so a user-set
    ``VISION_BASE_URL=http://localhost:11434/v1`` still resolves to the
    ``ollama`` server whose catalog host is ``127.0.0.1``. Returns ``None`` when
    no catalog entry matches (the caller then tries both unloads).
    """
    hp = _host_port(base_url)
    if hp is None:
        return None
    host, port = hp
    for server in config.SERVERS.values():
        if not server.runner:
            continue
        if _normalize_host(server.host) == host and server.port == port:
            return server.runner
    return None


def _post(url: str, payload: dict | None) -> None:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT):
        pass


def release_model(runner: str, base_url: str, model: str | None = None) -> None:
    """Release ``model`` from ``runner`` at ``base_url``; best-effort, never raises."""
    if runner == "ollama":
        if not model:
            return
        try:
            _post(f"{_root(base_url)}/api/generate", {"model": model, "keep_alive": 0})
        except Exception:  # noqa: BLE001 - a failed unload only means the model lingers
            return
    elif runner == "mlx-vlm":
        try:
            _post(f"{_root(base_url)}/unload", None)
        except Exception:  # noqa: BLE001
            return
    # Unknown runners are a no-op: we don't know how (or whether) to unload them.


def release(base_url: str, model: str | None = None) -> None:
    """Resolve ``base_url`` to a runner and unload ``model``, or try both unloads."""
    runner = resolve_runner(base_url)
    if runner is not None:
        release_model(runner, base_url, model)
        return
    # Unresolved endpoint: try both unload paths; each is a no-op when it doesn't
    # apply (Ollama needs a model, mlx-vlm's /unload only exists there), so the
    # double attempt is idempotent and safe.
    release_model("ollama", base_url, model)
    release_model("mlx-vlm", base_url, model)


def _writer_enabled() -> bool:
    return (
        config.is_enabled("format")
        or config.is_enabled("interpret")
        or config.is_enabled("structure")
        or config.is_enabled("summary")
    )


def release_readers() -> None:
    """Release the reader + classifier before the writer loads into freed memory.

    No-op unless a reader feature (vision/classify) AND a writer feature are
    enabled, and each reader is released only when it differs from the writer
    target (the default all-Qwen case releases nothing).
    """
    if not (config.is_enabled("vision") or config.is_enabled("classify")):
        return
    if not _writer_enabled():
        return
    from converter.classify import VISION_CLASSIFY_BASE_URL, VISION_CLASSIFY_MODEL
    from converter.vision import VISION_BASE_URL, VISION_MODEL
    from converter.write import WRITE_BASE_URL, WRITE_MODEL

    writer = (WRITE_BASE_URL, WRITE_MODEL)
    if config.is_enabled("vision") and (VISION_BASE_URL, VISION_MODEL) != writer:
        release(VISION_BASE_URL, VISION_MODEL)
    if config.is_enabled("classify") and (VISION_CLASSIFY_BASE_URL, VISION_CLASSIFY_MODEL) != writer:
        release(VISION_CLASSIFY_BASE_URL, VISION_CLASSIFY_MODEL)


def release_writers() -> None:
    """Release the writer + embeddings so nothing lingers into the next conversion.

    No-op when no writer feature is enabled. The writer is released only when it
    differs from the reader, so the single-model default (reader == writer) keeps
    its model resident rather than forcing a reload on the next conversion.
    """
    if not _writer_enabled():
        return
    from converter.summary import EMBED_BASE_URL, EMBED_MODEL
    from converter.vision import VISION_BASE_URL, VISION_MODEL
    from converter.write import WRITE_BASE_URL, WRITE_MODEL

    if (WRITE_BASE_URL, WRITE_MODEL) != (VISION_BASE_URL, VISION_MODEL):
        release(WRITE_BASE_URL, WRITE_MODEL)
    if config.is_enabled("summary"):
        release(EMBED_BASE_URL, EMBED_MODEL)
