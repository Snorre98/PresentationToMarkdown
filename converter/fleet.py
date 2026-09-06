"""Client for the macos-dev-config control daemon (ADR-0029).

The control daemon is the machine-local LLM control plane: the sole reader of
``models.json`` and the HTTP lifecycle surface over it (``list``/``status``/
``start``/``stop``/``provision``/``log``/``reach`` — its canonical contract is
``macos-dev-config/docs/contracts/daemon-http.md``). PresentationToMarkdown
consumes it for model discovery, health, on-demand start commands, and runner
identity; the static ``config.SERVERS`` defaults remain the offline fallback
projection of the same manifest.

Everything here is best-effort and never raises: an unreachable daemon means the
callers fall back to their static defaults, which is the pre-daemon behaviour.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from urllib.parse import urlparse

DAEMON_URL = os.environ.get("DAEMON_URL", "http://127.0.0.1:9300").rstrip("/")

_TIMEOUT = 1.5
_CACHE_TTL = 30.0

_cache: dict[str, dict] = {}
_cached_at: float = 0.0


def _get(path: str, timeout: float = _TIMEOUT) -> dict | None:
    """GET one daemon verb as parsed JSON; ``None`` when unreachable/invalid."""
    req = urllib.request.Request(f"{DAEMON_URL}{path}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if not 200 <= resp.status < 300:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - any failure means "daemon not answering"
        return None


def list_models(force: bool = False) -> dict[str, dict]:
    """The daemon's ``list`` projection as ``{model name: entry}``, cached.

    ``entry`` carries ``name``, ``runner``, ``daemon``, ``host``, ``port``,
    ``capabilities``, ``defaults``, ``modeTags`` (daemon-http.md §2). On daemon
    failure the last good cache is kept (possibly empty).
    """
    global _cache, _cached_at
    now = time.monotonic()
    if not force and _cache and now - _cached_at < _CACHE_TTL:
        return _cache
    rsp = _get("/list")
    if rsp is None:
        return _cache
    _cache = {entry["name"]: entry for entry in rsp.get("models", [])}
    _cached_at = now
    return _cache


def status(name: str) -> str:
    """One daemon/model's live state via ``GET /status/{name}``.

    ``"unknown"`` when the daemon is unreachable or the name is not in the
    manifest.
    """
    rsp = _get(f"/status/{name}")
    if rsp is None:
        return "unknown"
    return str(rsp.get("state", "unknown"))


def start_command(name: str) -> str:
    """The command a user runs to start ``name`` via the daemon's ``start`` verb."""
    return f"curl -X POST {DAEMON_URL}/start/{name}"


def base_url(name: str) -> str | None:
    """OpenAI-compatible base URL for a manifest daemon/model name, from ``list``.

    ``None`` when the daemon is unreachable or ``name`` is absent — callers fall
    back to their static default.
    """
    entry = list_models().get(name)
    if not entry:
        return None
    host = entry.get("host") or "127.0.0.1"
    return f"http://{host}:{entry['port']}/v1"


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


def runner_for(base_url: str) -> str | None:
    """Resolve an effective base URL to its manifest ``runner`` by host:port.

    The runner enum (``llama.cpp | mlx-lm | mlx-vlm | delegate``) drives the
    runner-specific memory-release paths in ``converter.lifecycle`` (ADR-0017).
    ``None`` when the daemon is unreachable or the URL matches no manifest entry.
    """
    hp = _host_port(base_url)
    if hp is None:
        return None
    for entry in list_models().values():
        host = _normalize_host(entry.get("host") or "127.0.0.1")
        if host == hp[0] and entry.get("port") == hp[1]:
            return entry.get("runner")
    return None
